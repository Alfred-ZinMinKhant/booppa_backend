from app.core.route_classes import RetryAPIRoute
"""
ropa_intake.py — TASK 1: /api/ropa/intake endpoints.

Follows the exact same auth pattern as app/api/rfp_intake.py (OAuth2PasswordBearer
+ verify_access_token + _resolve_user) for consistency — a buyer hitting this
endpoint is already authenticated the same way they are for RFP intake, no new
auth mechanism needed.

Register this router in main.py the same way rfp_intake's router is registered
(grep `include_router.*rfp_intake` in main.py to find the exact prefix used,
then mount this one at "/api/ropa").
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.auth import verify_access_token
from app.core.db import get_db
from app.core.models import User
from app.core.models import RopaActivities  # add RopaActivities to models_v12.py per ropa_models.py
from app.services.ropa_generator import ROPA_INTAKE_SCHEMA, PDPA_LEGAL_BASIS_OPTIONS, validate_ropa_intake

router = APIRouter(route_class=RetryAPIRoute)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def _resolve_user(token: str | None, db: Session) -> User:
    """Identical to rfp_intake.py's _resolve_user — duplicated here rather
    than imported across modules to avoid a cross-router import dependency
    for a 6-line function; if a shared auth_helpers module exists, use that
    instead of duplicating."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/schema")
def get_ropa_schema():
    """
    Return the 5-question schema + legal basis options so the frontend can
    render the form without hardcoding field labels/help text twice (once
    here, once in ropa_generator.py). Call this on page load.
    """
    return {
        "fields": ROPA_INTAKE_SCHEMA,
        "legal_basis_options": PDPA_LEGAL_BASIS_OPTIONS,
        "max_activities": 15,
    }


@router.get("/intake")
def get_draft_activities(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Return the buyer's current draft ROPA rows (for resuming a partially
    filled form) plus their submission status."""
    user = _resolve_user(token, db)
    rows = (
        db.query(RopaActivities)
        .filter(RopaActivities.user_id == user.id)
        .order_by(RopaActivities.created_at.asc())
        .all()
    )
    return {
        "activities": [
            {
                "id": str(r.id),
                "status": r.status,
                "processing_purpose": r.processing_purpose,
                "data_categories": r.data_categories,
                "data_subjects": r.data_subjects,
                "retention_period": r.retention_period,
                "cross_border_transfer": r.cross_border_transfer,
                "legal_basis": r.legal_basis,
            }
            for r in rows
        ],
        "submitted": any(r.status == "submitted" for r in rows),
    }


@router.post("/intake")
def save_draft_activities(
    body: dict,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Save (replace) the buyer's draft ROPA rows. Called as the buyer fills
    the multi-row form — safe to call repeatedly with the full current set
    of rows (upsert-by-replace, not append), matching how a multi-row form
    typically autosaves. Does NOT trigger PDF generation — that happens in
    /intake/submit.

    Body: {"activities": [ {processing_purpose, data_categories,
                             data_subjects, retention_period,
                             cross_border_transfer, legal_basis}, ... ] }
    """
    user = _resolve_user(token, db)
    activities = body.get("activities")
    if not isinstance(activities, list):
        raise HTTPException(status_code=422, detail="'activities' must be a list.")

    errors = validate_ropa_intake(activities)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    # Replace-all: delete existing draft rows for this user, insert the
    # current set. Simpler and safer than diffing for a form with at most
    # 15 rows — avoids partial-update bugs when the buyer reorders/deletes
    # rows in the UI.
    existing_submitted = (
        db.query(RopaActivities)
        .filter(RopaActivities.user_id == user.id, RopaActivities.status == "submitted")
        .first()
    )
    if existing_submitted:
        raise HTTPException(
            status_code=409,
            detail="ROPA has already been submitted for this account and cannot be edited. Contact support for changes.",
        )

    db.query(RopaActivities).filter(
        RopaActivities.user_id == user.id, RopaActivities.status == "draft"
    ).delete()

    for row in activities:
        db.add(RopaActivities(
            user_id=user.id,
            bundle_source="compliance_evidence_pack",
            status="draft",
            processing_purpose=row["processing_purpose"].strip(),
            data_categories=row["data_categories"].strip(),
            data_subjects=row["data_subjects"].strip(),
            retention_period=row["retention_period"].strip(),
            cross_border_transfer=row["cross_border_transfer"].strip(),
            legal_basis=row["legal_basis"].strip(),
        ))
    db.commit()
    return {"saved": len(activities), "status": "draft"}


@router.post("/intake/submit")
def submit_ropa_activities(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Finalise the buyer's draft ROPA rows: flip status to 'submitted' and
    queue ROPA PDF generation + inclusion in the next Cover Sheet cycle.

    Mirrors rfp_intake.py's submit_intake row-lock pattern (with_for_update)
    to prevent a double-submit race if the buyer double-clicks Generate.
    """
    user = _resolve_user(token, db)

    rows = (
        db.query(RopaActivities)
        .filter(RopaActivities.user_id == user.id, RopaActivities.status == "draft")
        .with_for_update()
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=422,
            detail="No processing activities declared yet. Add at least one before submitting.",
        )

    already_submitted = (
        db.query(RopaActivities)
        .filter(RopaActivities.user_id == user.id, RopaActivities.status == "submitted")
        .first()
    )
    if already_submitted:
        raise HTTPException(status_code=409, detail="ROPA has already been submitted for this account.")

    now = datetime.utcnow()
    for r in rows:
        r.status = "submitted"
        r.submitted_at = now
    db.commit()

    # Queue ROPA generation. This does NOT directly trigger the Cover Sheet —
    # it sets the data up so the NEXT fulfill_cover_sheet_task run (auto-fired
    # by the existing pdpa_done/rfp_done gate in stripe_webhook.py, see
    # ropa_fulfillment_patch.py for the exact wiring) finds submitted ROPA
    # rows and includes them. If PDPA + RFP are already done and the buyer
    # is only now finishing ROPA, fire the cover sheet generation explicitly
    # here instead of waiting for an unrelated trigger:
    from app.core.models import Report
    pdpa_done = (
        db.query(Report)
        .filter(Report.owner_id == user.id, Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                 Report.status == "completed")
        .first() is not None
    )
    rfp_done = bool(getattr(user, "compliance_evidence_rfp_ready", False))
    if pdpa_done and rfp_done:
        from app.workers.tasks import fulfill_cover_sheet_task
        company_name = (user.company or "").strip() or "Your Organisation"
        # NOTE: fulfill_cover_sheet_task's own docstring says it normally
        # runs ~300s after bundle components are queued, to give PDPA/RFP
        # generation time to finish. That doesn't apply here — this branch
        # only fires when pdpa_done and rfp_done are BOTH already true, so
        # there's nothing left to wait for except the ROPA PDF this same
        # request just queued. A short countdown (not 0) just avoids a
        # race against the DB commit above being visible to the worker.
        fulfill_cover_sheet_task.apply_async(
            kwargs={
                "bundle_type": "compliance_evidence_pack",
                "customer_email": user.email,
                "company_name": company_name,
                "metadata": {"auto_fired": True, "ropa_just_submitted": True},
            },
            countdown=10,
        )
        logger.info(f"[ROPA] Submitted + cover sheet re-fired for {user.email} (PDPA+RFP already done)")
    else:
        logger.info(f"[ROPA] Submitted for {user.email}, awaiting PDPA/RFP completion before cover sheet fires")

    return {"submitted": len(rows), "status": "submitted"}
