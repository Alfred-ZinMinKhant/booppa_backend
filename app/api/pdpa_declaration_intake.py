"""
pdpa_declaration_intake.py — /api/pdpa-declaration endpoints (PDPA Level-2).

Same auth + draft/submit pattern as ropa_intake.py. On submit, queues
fulfill_pdpa_declaration_task which renders, anchors, and emails the declaration.
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
from app.core.pdpa_declaration_models import PdpaSelfDeclaration
from app.services.pdpa_declaration_generator import (
    PDPA_DECLARATION_SCHEMA,
    PDPA_LEGAL_BASIS_OPTIONS,
    validate_pdpa_declaration,
)

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

_FIELD_KEYS = [f["key"] for f in PDPA_DECLARATION_SCHEMA]


def _resolve_user(token: str | None, db: Session) -> User:
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
def get_schema():
    return {
        "fields": PDPA_DECLARATION_SCHEMA,
        "legal_basis_options": PDPA_LEGAL_BASIS_OPTIONS,
        "max_activities": 20,
    }


@router.get("/status")
def get_status(token: str | None = Security(oauth2_scheme), db: Session = Depends(get_db)):
    """Level-2 standing for the PDPA report page: whether the declaration is
    drafted/submitted, and once fulfilled, the anchored record (fresh download
    URL + blockchain tx) so the report can present it as part of the deliverable."""
    from app.core.models import Report
    from app.core.config import settings

    user = _resolve_user(token, db)
    decls = (
        db.query(PdpaSelfDeclaration)
        .filter(PdpaSelfDeclaration.user_id == user.id)
        .all()
    )
    has_draft = any(d.status == "draft" for d in decls)
    submitted = any(d.status == "submitted" for d in decls)

    report = (
        db.query(Report)
        .filter(Report.owner_id == user.id, Report.framework == "pdpa_self_declaration")
        .order_by(Report.created_at.desc())
        .first()
    )

    download_url = None
    tx_hash = None
    anchored_at = None
    if report:
        tx_hash = report.tx_hash
        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        anchored_at = ad.get("blockchain_anchored_at")
        s3_key = ad.get("s3_key")
        if s3_key:
            try:
                from app.services.storage import S3Service
                s3 = S3Service()
                download_url = s3.s3_client.generate_presigned_url(
                    "get_object", Params={"Bucket": s3.bucket, "Key": s3_key}, ExpiresIn=604800,
                )
            except Exception:
                download_url = ad.get("s3_url")

    return {
        "has_draft": has_draft,
        "submitted": submitted,
        "completed": report is not None,
        "download_url": download_url,
        "tx_hash": tx_hash,
        "anchored_at": anchored_at,
        "network": settings.active_polygon_network_name,
        "explorer_url": settings.active_polygon_explorer_url.rstrip("/"),
    }


@router.get("/intake")
def get_draft(token: str | None = Security(oauth2_scheme), db: Session = Depends(get_db)):
    user = _resolve_user(token, db)
    rows = (
        db.query(PdpaSelfDeclaration)
        .filter(PdpaSelfDeclaration.user_id == user.id)
        .order_by(PdpaSelfDeclaration.created_at.asc())
        .all()
    )
    return {
        "activities": [
            {"id": str(r.id), "status": r.status, **{k: getattr(r, k) for k in _FIELD_KEYS}}
            for r in rows
        ],
        "submitted": any(r.status == "submitted" for r in rows),
    }


@router.post("/intake")
def save_draft(body: dict, token: str | None = Security(oauth2_scheme), db: Session = Depends(get_db)):
    user = _resolve_user(token, db)
    activities = body.get("activities")
    if not isinstance(activities, list):
        raise HTTPException(status_code=422, detail="'activities' must be a list.")

    errors = validate_pdpa_declaration(activities)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    existing_submitted = (
        db.query(PdpaSelfDeclaration)
        .filter(PdpaSelfDeclaration.user_id == user.id, PdpaSelfDeclaration.status == "submitted")
        .first()
    )
    if existing_submitted:
        raise HTTPException(
            status_code=409,
            detail="PDPA self-declaration already submitted for this account and cannot be edited. Contact support for changes.",
        )

    db.query(PdpaSelfDeclaration).filter(
        PdpaSelfDeclaration.user_id == user.id, PdpaSelfDeclaration.status == "draft"
    ).delete()

    for row in activities:
        db.add(PdpaSelfDeclaration(
            user_id=user.id,
            source="pdpa_quick_scan",
            status="draft",
            **{k: (row.get(k) or "").strip() for k in _FIELD_KEYS},
        ))
    db.commit()
    return {"saved": len(activities), "status": "draft"}


@router.post("/intake/submit")
def submit(token: str | None = Security(oauth2_scheme), db: Session = Depends(get_db)):
    user = _resolve_user(token, db)

    rows = (
        db.query(PdpaSelfDeclaration)
        .filter(PdpaSelfDeclaration.user_id == user.id, PdpaSelfDeclaration.status == "draft")
        .with_for_update()
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=422,
            detail="No processing activities declared yet. Add at least one before submitting.",
        )

    already = (
        db.query(PdpaSelfDeclaration)
        .filter(PdpaSelfDeclaration.user_id == user.id, PdpaSelfDeclaration.status == "submitted")
        .first()
    )
    if already:
        raise HTTPException(status_code=409, detail="PDPA self-declaration already submitted.")

    now = datetime.utcnow()
    for r in rows:
        r.status = "submitted"
        r.submitted_at = now
    db.commit()

    from app.workers.tasks import fulfill_pdpa_declaration_task
    fulfill_pdpa_declaration_task.apply_async(
        kwargs={"user_id": str(user.id), "customer_email": user.email}, countdown=5
    )
    logger.info("[PDPADeclaration] Submitted + fulfillment queued for %s", user.email)
    return {"submitted": len(rows), "status": "submitted"}
