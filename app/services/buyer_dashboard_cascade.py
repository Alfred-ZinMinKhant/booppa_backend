"""
buyer_dashboard_cascade.py — Buyer Dashboard Cascade Service.

Enforces legal authorization gate (BuyerNameUsageAuthorization) before sending
notifications on buyer's behalf to vendor pools.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_current_user, get_db
from app.core.models import BuyerNameUsageAuthorization, User

router = APIRouter(prefix="/buyer-dashboard", tags=["buyer-dashboard"])


class AuthorizeNameUsageRequest(BaseModel):
    accepted: bool


@router.post("/authorize-name-usage")
async def authorize_name_usage(
    body: AuthorizeNameUsageRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.accepted:
        raise HTTPException(status_code=422, detail="Authorization requires explicit acceptance (accepted=true)")

    auth = BuyerNameUsageAuthorization(
        buyer_user_id=current_user.id,
        authorization_version="1.0",
        clause_text_shown=CLAUSE_TEXT_CURRENT_VERSION,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(auth)
    db.commit()
    return {"status": "authorized", "authorization_id": str(auth.id)}


@router.post("/revoke-name-usage")
async def revoke_name_usage(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    auth = (
        db.query(BuyerNameUsageAuthorization)
        .filter(
            BuyerNameUsageAuthorization.buyer_user_id == current_user.id,
            BuyerNameUsageAuthorization.revoked_at.is_(None),
        )
        .first()
    )
    if not auth:
        raise HTTPException(status_code=404, detail="No active authorization to revoke")

    auth.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "revoked"}


@router.post("/cascade/preview")
async def preview_cascade(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    from app.services.evidence_enricher import display_legal_name
    buyer_company = display_legal_name(current_user, db) or current_user.email
    results = run_buyer_vendor_cascade(
        db, str(current_user.id), buyer_company, dry_run=True
    )
    return {"preview": [r.__dict__ for r in results]}


@router.post("/cascade/send")
async def send_cascade(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    from app.services.evidence_enricher import display_legal_name
    buyer_company = display_legal_name(current_user, db) or current_user.email
    results = run_buyer_vendor_cascade(
        db, str(current_user.id), buyer_company, dry_run=False
    )

    if len(results) == 1 and results[0].skip_reason == CascadeSkipReason.NO_AUTHORIZATION:
        raise HTTPException(
            status_code=403,
            detail="Buyer Dashboard cascade requires an active name-usage authorization.",
        )
    return {"results": [r.__dict__ for r in results]}


CLAUSE_TEXT_CURRENT_VERSION = """
I authorize Booppa Smart Care LLC to use my company's name in
communications addressed to the vendors in my verification pool, for the
specific purpose of requesting that they obtain a Trust Passport as a
condition of remaining qualified in my procurement process.
""".strip()

DEADLINE_DAYS = 30
NOTIFICATION_COOLDOWN_DAYS = 14


def has_active_name_usage_authorization(db, buyer_user_id: str) -> bool:
    from app.core.models import BuyerNameUsageAuthorization
    return db.query(
        db.query(BuyerNameUsageAuthorization)
        .filter(
            BuyerNameUsageAuthorization.buyer_user_id == buyer_user_id,
            BuyerNameUsageAuthorization.revoked_at.is_(None),
        )
        .exists()
    ).scalar()


def was_recently_notified(
    db, buyer_user_id: str, vendor_email: str, cooldown_days: int = NOTIFICATION_COOLDOWN_DAYS
) -> bool:
    from app.core.models import BuyerCascadeNotificationLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    return db.query(
        db.query(BuyerCascadeNotificationLog)
        .filter(
            BuyerCascadeNotificationLog.buyer_user_id == buyer_user_id,
            BuyerCascadeNotificationLog.vendor_email == vendor_email,
            BuyerCascadeNotificationLog.sent_at >= cutoff,
        )
        .exists()
    ).scalar()


def log_cascade_notification(db, buyer_user_id: str, vendor_email: str) -> None:
    from app.core.models import BuyerCascadeNotificationLog

    db.add(BuyerCascadeNotificationLog(buyer_user_id=buyer_user_id, vendor_email=vendor_email))
    db.commit()


class CascadeSkipReason(str, Enum):
    NO_AUTHORIZATION = "no_active_authorization"
    ALREADY_HAS_PASSPORT = "vendor_already_has_passport"
    ALREADY_NOTIFIED_RECENTLY = "notified_within_cooldown"
    NO_EMAIL_ON_FILE = "no_vendor_email"


@dataclass
class CascadeNotificationResult:
    vendor_email: Optional[str]
    vendor_name: str
    sent: bool
    skip_reason: Optional[CascadeSkipReason] = None


def build_cascade_notification_text(
    buyer_company_name: str, vendor_name: str, deadline_days: int = DEADLINE_DAYS
) -> dict:
    subject = f"{buyer_company_name} — Trust Passport required within {deadline_days} days"
    body_inner = f"""
    <p>Dear {vendor_name},</p>
    <p><strong>{buyer_company_name}</strong> has adopted the Booppa Vendor
    Verification standard. To remain qualified in their vendor pool, a
    Trust Passport is required within {deadline_days} days.</p>
    <p>A Trust Passport takes minutes to generate and gives {buyer_company_name}
    a verified view of your compliance readiness — no lengthy questionnaire,
    no back-and-forth.</p>
    """
    return {"subject": subject, "body_html_inner": body_inner}


def run_buyer_vendor_cascade(
    db, buyer_user_id: str, buyer_company_name: str, dry_run: bool = False
) -> list[CascadeNotificationResult]:
    if not has_active_name_usage_authorization(db, buyer_user_id):
        return [
            CascadeNotificationResult(
                vendor_email=None,
                vendor_name="(entire pool)",
                sent=False,
                skip_reason=CascadeSkipReason.NO_AUTHORIZATION,
            )
        ]

    from app.core.models import ManagedVendor, Report

    pool = (
        db.query(ManagedVendor)
        .filter(
            ManagedVendor.enterprise_user_id == buyer_user_id,
            ManagedVendor.status == "ACTIVE",
        )
        .all()
    )

    results = []
    for mv in pool:
        if not mv.vendor_email:
            results.append(
                CascadeNotificationResult(
                    vendor_email=None,
                    vendor_name=mv.vendor_name or "(unknown)",
                    sent=False,
                    skip_reason=CascadeSkipReason.NO_EMAIL_ON_FILE,
                )
            )
            continue

        has_passport = db.query(
            db.query(Report)
            .filter(Report.company_name == (mv.vendor_name or ""), Report.framework == "trust_passport")
            .exists()
        ).scalar()
        if has_passport:
            results.append(
                CascadeNotificationResult(
                    vendor_email=mv.vendor_email,
                    vendor_name=mv.vendor_name,
                    sent=False,
                    skip_reason=CascadeSkipReason.ALREADY_HAS_PASSPORT,
                )
            )
            continue

        if was_recently_notified(db, buyer_user_id, mv.vendor_email):
            results.append(
                CascadeNotificationResult(
                    vendor_email=mv.vendor_email,
                    vendor_name=mv.vendor_name,
                    sent=False,
                    skip_reason=CascadeSkipReason.ALREADY_NOTIFIED_RECENTLY,
                )
            )
            continue

        text = build_cascade_notification_text(buyer_company_name, mv.vendor_name or mv.vendor_email)

        if dry_run:
            results.append(
                CascadeNotificationResult(
                    vendor_email=mv.vendor_email, vendor_name=mv.vendor_name, sent=False
                )
            )
            continue

        log_cascade_notification(db, buyer_user_id, mv.vendor_email)
        results.append(
            CascadeNotificationResult(
                vendor_email=mv.vendor_email, vendor_name=mv.vendor_name, sent=True
            )
        )

    return results
