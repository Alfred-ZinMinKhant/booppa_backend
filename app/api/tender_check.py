"""
Tender Check API — V10
======================
GET  /api/v1/tender-check          — probability calculation
POST /api/v1/tender-check/claim    — lightweight profile claim for leads
"""

import secrets
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.tender_service import compute_tender_win_probability

logger = logging.getLogger(__name__)

router = APIRouter()


def _log_tender_check_lookup(db: Session, tender_no: str, vendor_id: Optional[str], result: dict) -> None:
    """Append-only log of /tender-check calls for the Vendor Pro
    competitor-awareness signal. Respects per-user opt-out and swallows
    errors so a logging failure never breaks the user-facing call."""
    try:
        from app.core.models import TenderCheckLookup
        from app.core.models import User
        from app.core.models import VerifyRecord  # type: ignore

        is_verified = False
        opted_out = False
        if vendor_id:
            user = db.query(User).filter(User.id == vendor_id).first()
            if user:
                opted_out = bool(getattr(user, "tender_lookup_opt_out", False))
                # Crude verified check: presence of a VerifyRecord row.
                try:
                    is_verified = (
                        db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
                        is not None
                    )
                except Exception:
                    is_verified = False
        if opted_out:
            return
        db.add(TenderCheckLookup(
            tender_no=tender_no,
            vendor_id=vendor_id if vendor_id else None,
            sector=(result or {}).get("sector"),
            is_verified=is_verified,
        ))
        db.commit()

        # Threshold-based real-time push: if lookups on this tender in the last
        # hour cross 3 (with at least one verified), fire an SSE event so any
        # Vendor Pro dashboard subscribed to the stream renders a live signal.
        try:
            from datetime import datetime, timedelta, timezone
            from app.api.sse import publish_event
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            recent = (
                db.query(TenderCheckLookup)
                .filter(
                    TenderCheckLookup.tender_no == tender_no,
                    TenderCheckLookup.created_at >= since.replace(tzinfo=None),
                )
                .all()
            )
            if len(recent) >= 3 and any(r.is_verified for r in recent):
                publish_event("competitor_signal", {
                    "tender_no": tender_no,
                    "lookups_last_hour": len(recent),
                    "verified_lookups_last_hour": sum(1 for r in recent if r.is_verified),
                    "sector": (result or {}).get("sector"),
                    "agency": (result or {}).get("agency"),
                })
        except Exception as sse_err:
            logger.debug(f"[TenderCheckLookup] SSE publish skipped: {sse_err}")
    except Exception as e:
        # Logging is best-effort. Roll back any partial state and continue.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"[TenderCheckLookup] failed to log lookup for tender={tender_no}: {e}")


@router.get("")
def tender_check(
    tenderNo: str = Query(..., description="GeBIZ tender number to evaluate"),
    vendorId: Optional[str] = Query(None, description="Vendor UUID (optional — omit for guest view)"),
    db: Session = Depends(get_db),
):
    """
    Estimate a vendor's probability of winning a GeBIZ tender.

    - **tenderNo**: Required. The GeBIZ tender reference (e.g. ``ITQ202500001``).
    - **vendorId**: Optional. Provide to receive personalised probability,
      projections, and gap analysis. Omit for a sector-baseline view.
    """
    result = compute_tender_win_probability(db, tenderNo, vendorId)

    if result.get("error") == "tender_not_found":
        raise HTTPException(status_code=404, detail=f"Tender '{tenderNo}' not found in shortlist")

    # Append a row to TenderCheckLookup (Vendor Pro competitor-awareness signal).
    _log_tender_check_lookup(db, tenderNo, vendorId, result)

    return result


class ClaimProfileRequest(BaseModel):
    company_name: str
    email: EmailStr
    uen: Optional[str] = None
    tender_no: Optional[str] = None


@router.post("/claim")
def claim_profile(body: ClaimProfileRequest, db: Session = Depends(get_db)):
    """
    Lightweight profile claim — creates a lead vendor account so the user
    gets personalised tender probability without full registration.
    """
    from app.core.models import User
    from app.core.models import VendorStatusSnapshot
    from app.core.models import LeadCapture
    from app.core.auth import get_password_hash

    # Check if user already exists with this email
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        return {"vendor_id": str(existing.id), "profile_created": False, "message": "Profile already exists"}

    # Create lightweight user with temp password
    temp_pw = secrets.token_urlsafe(24)
    user = User(
        id=uuid.uuid4(),
        email=body.email,
        hashed_password=get_password_hash(temp_pw),
        full_name=body.company_name,
        company=body.company_name,
        uen=body.uen if body.uen else None,
        role="VENDOR",
        plan="free",
        temp_password=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    # Create initial VendorStatusSnapshot
    snapshot = VendorStatusSnapshot(
        id=uuid.uuid4(),
        vendor_id=user.id,
        verification_depth="UNVERIFIED",
        monitoring_activity="NONE",
        risk_signal="CLEAN",
        procurement_readiness="NOT_READY",
        risk_adjusted_pct=50.0,
        evidence_count=0,
        confidence_score=0.0,
        computed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)

    # Save lead capture
    lead = LeadCapture(
        id=uuid.uuid4(),
        email=body.email,
        company=body.company_name,
        uen=body.uen,
        correlation_id=f"tender_claim:{body.tender_no or 'direct'}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(lead)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[TenderClaim] Failed to create profile: {e}")
        raise HTTPException(status_code=409, detail="Could not create profile. Email or UEN may already be in use.")

    logger.info(f"[TenderClaim] Created lead vendor {user.id} for {body.email}")
    return {"vendor_id": str(user.id), "profile_created": True, "message": "Profile claimed successfully"}
