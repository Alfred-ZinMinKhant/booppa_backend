"""RFP intake endpoints for bundle buyers.

Bundle SKUs that include an RFP component defer kit generation: at webhook time
we create a PendingRfpIntake row and email the buyer a link. This module backs
those endpoints — list pending intakes and submit a brief.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.auth import verify_access_token
from app.core.db import get_db
from app.core.models import User
from app.core.models_v12 import PendingRfpIntake

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


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


@router.get("/pending")
def list_pending(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """List the authenticated user's pending RFP intakes (one per bundle purchase)."""
    user = _resolve_user(token, db)
    rows = (
        db.query(PendingRfpIntake)
        .filter(
            PendingRfpIntake.user_id == user.id,
            PendingRfpIntake.status == "pending",
        )
        .order_by(PendingRfpIntake.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "rfp_product_type": r.rfp_product_type,
                "bundle_source": r.bundle_source,
                "vendor_url": r.vendor_url,
                "company_name": r.company_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/{intake_id}")
def get_intake(
    intake_id: str,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Fetch a single pending intake. Used by the intake page on /rfp-intake/{id}."""
    user = _resolve_user(token, db)
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")
    return {
        "id": str(row.id),
        "rfp_product_type": row.rfp_product_type,
        "bundle_source": row.bundle_source,
        "vendor_url": row.vendor_url,
        "company_name": row.company_name,
        "status": row.status,
        # session_id lets the intake page route back to /rfp-acceleration/result
        # after submit so the buyer sees the kit polling/result, not just a
        # "go to dashboard" dead end.
        "session_id": row.session_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
    }


@router.post("/{intake_id}/submit")
def submit_intake(
    intake_id: str,
    body: dict,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Submit the RFP brief and queue fulfill_rfp_task.

    Body: { rfp_description: str (required), intake_data?: dict, sector?: str }
    """
    user = _resolve_user(token, db)
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="This RFP has already been submitted")

    rfp_description = (body.get("rfp_description") or "").strip()
    if not rfp_description:
        raise HTTPException(status_code=422, detail="rfp_description is required")
    intake_data = body.get("intake_data") if isinstance(body.get("intake_data"), dict) else None
    sector = body.get("sector")

    vendor_url = row.vendor_url or (getattr(user, "website", "") or "")
    company_name = row.company_name or (getattr(user, "company", "") or "")

    row.status = "submitted"
    row.submitted_at = datetime.utcnow()
    db.commit()

    from app.workers.tasks import fulfill_rfp_task

    fulfill_rfp_task.delay(
        product_type=row.rfp_product_type,
        vendor_id=str(user.id),
        vendor_email=user.email,
        vendor_url=vendor_url,
        company_name=company_name,
        rfp_description=rfp_description,
        session_id=row.session_id,
        intake_data=intake_data,
    )

    # Strategy 6 — notify top sector peers, mirrors the standalone rfp_express path.
    if row.rfp_product_type == "rfp_express":
        try:
            from app.workers.tasks import fire_strategy_6_task

            fire_strategy_6_task.delay(sector, rfp_description)
        except Exception:
            pass

    return {
        "status": "queued",
        "intake_id": str(row.id),
        "session_id": row.session_id,
    }
