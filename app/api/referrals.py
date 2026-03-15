"""
Referral API Routes
===================
P9 referral program endpoints.
"""

import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models_v10 import Referral

router = APIRouter()


class CreateReferralRequest(BaseModel):
    referrer_id: str
    referred_email: Optional[str] = None


@router.post("/create")
async def create_referral(body: CreateReferralRequest, db: Session = Depends(get_db)):
    """Create a referral code."""
    # Check if referrer already has an active code
    existing = db.query(Referral).filter(
        Referral.referrer_id == body.referrer_id,
        Referral.status == "PENDING",
    ).first()

    if existing:
        return {
            "referral_code": existing.referral_code,
            "status": existing.status,
            "message": "Existing referral code returned",
        }

    code = secrets.token_urlsafe(8).upper()[:10]
    referral = Referral(
        referrer_id=body.referrer_id,
        referral_code=code,
        referred_email=body.referred_email,
        expires_at=datetime.utcnow() + timedelta(days=90),
    )
    db.add(referral)
    db.commit()

    return {
        "referral_code": code,
        "expires_at": referral.expires_at.isoformat(),
        "status": "PENDING",
    }


@router.get("/code/{code}")
async def get_referral(code: str, db: Session = Depends(get_db)):
    """Get referral details by code."""
    referral = db.query(Referral).filter(Referral.referral_code == code).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral code not found")

    return {
        "referral_code": referral.referral_code,
        "referrer_id": str(referral.referrer_id),
        "referred_id": str(referral.referred_id) if referral.referred_id else None,
        "status": referral.status,
        "reward_type": referral.reward_type,
        "reward_claimed": referral.reward_claimed,
        "expires_at": referral.expires_at.isoformat() if referral.expires_at else None,
    }


@router.post("/redeem/{code}")
async def redeem_referral(code: str, user_id: str, db: Session = Depends(get_db)):
    """Redeem a referral code during signup."""
    referral = db.query(Referral).filter(Referral.referral_code == code).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral code not found")

    if referral.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Referral already in status: {referral.status}")

    if referral.expires_at and referral.expires_at < datetime.utcnow():
        referral.status = "EXPIRED"
        db.commit()
        raise HTTPException(status_code=400, detail="Referral code has expired")

    referral.referred_id = user_id
    referral.status = "SIGNED_UP"
    db.commit()

    return {"message": "Referral redeemed", "status": "SIGNED_UP"}


@router.get("/my/{user_id}")
async def list_my_referrals(user_id: str, db: Session = Depends(get_db)):
    """List all referrals created by a user."""
    referrals = db.query(Referral).filter(Referral.referrer_id == user_id).all()
    return [
        {
            "referral_code": r.referral_code,
            "status": r.status,
            "referred_email": r.referred_email,
            "reward_claimed": r.reward_claimed,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in referrals
    ]
