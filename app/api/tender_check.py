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
    from app.core.models_v8 import VendorStatusSnapshot
    from app.core.models_v6 import LeadCapture
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
