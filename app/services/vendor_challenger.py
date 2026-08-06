"""
vendor_challenger.py — Vendor Challenger Service (GTM v9).

Allows vendors to invite buyers to verify their public Trust Passport.
Uses User.company field (fixed from non-existent company_name).
"""

from dataclasses import dataclass
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from app.core.config import settings
from app.core.db import get_current_user, get_db
from app.core.models import User
from app.services.email_layout import branded_email_html, email_button
from app.services.email_service import EmailService

router = APIRouter(prefix="/vendor-challenger", tags=["vendor-challenger"])


class ChallengeBuyerRequest(BaseModel):
    buyer_email: EmailStr
    buyer_name: Optional[str] = None


@router.post("/challenge")
async def send_vendor_challenge(
    body: ChallengeBuyerRequest,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.uen:
        raise HTTPException(status_code=422, detail="Vendor profile has no UEN")

    req = ChallengeRequest(
        vendor_uen=current_user.uen,
        vendor_company_name=current_user.company or current_user.email,
        vendor_user_id=str(current_user.id),
        buyer_email=body.buyer_email,
        buyer_name=body.buyer_name,
    )
    composed = validate_and_build_challenge(req, getattr(settings, "VERIFY_BASE_URL", "https://booppa.io"))

    button_html = email_button(composed["passport_url"], "View Trust Passport", primary=True)
    full_body = branded_email_html(
        composed["body_html_inner"] + button_html,
        title=f"{req.vendor_company_name} Trust Passport",
        preheader="Verify compliance in 30 seconds — no account needed",
    )

    email_svc = EmailService()
    sent = await email_svc.send_html_email(
        to_email=composed["to_email"],
        subject=composed["subject"],
        body_html=full_body,
        category="vendor_challenger",
    )
    if not sent:
        raise HTTPException(status_code=502, detail="Email delivery failed")

    return {"status": "sent", "passport_url": composed["passport_url"]}



@dataclass
class ChallengeRequest:
    vendor_uen: str
    vendor_company_name: str
    vendor_user_id: str
    buyer_email: str
    buyer_name: Optional[str] = None


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


def build_challenge_passport_url(vendor_uen: str, verify_base_url: str) -> str:
    return f"{verify_base_url.rstrip('/')}/passport/profile/{vendor_uen}"


def compose_challenge_email(request: ChallengeRequest, passport_url: str) -> dict:
    subject = f"{request.vendor_company_name} — verify our compliance in 30 seconds"
    greeting = f"Dear {request.buyer_name}," if request.buyer_name else "Hello,"

    body_inner = f"""
    <p>{greeting}</p>
    <p><strong>{request.vendor_company_name}</strong> has invited you to verify their
    compliance readiness — no Booppa account required, no questionnaire to
    complete, no email follow-up needed.</p>
    <p>You will see in one page: ACRA registration status, PDPA compliance
    score across 11 dimensions, GeBIZ contract history, and
    blockchain-anchored evidence of their self-declarations.</p>
    <p>If you decide to work with them, their Trust Passport is already
    formatted for your standard vendor onboarding process.</p>
    """

    return {
        "to_email": request.buyer_email,
        "subject": subject,
        "body_html_inner": body_inner,
        "passport_url": passport_url,
        "category": "vendor_challenger",
    }


def validate_and_build_challenge(request: ChallengeRequest, verify_base_url: str) -> dict:
    if not _is_valid_email(request.buyer_email):
        raise ValueError(f"Invalid buyer email: {request.buyer_email!r}")
    if not request.vendor_uen or not request.vendor_uen.strip():
        raise ValueError("vendor_uen missing — a Vendor Challenger requires a real UEN")

    passport_url = build_challenge_passport_url(request.vendor_uen, verify_base_url)
    return compose_challenge_email(request, passport_url)
