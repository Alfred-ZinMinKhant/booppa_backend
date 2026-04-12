from fastapi import APIRouter, HTTPException
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.core.config import settings
from app.services.blockchain import BlockchainService
from app.services.email_service import EmailService
import asyncio
import logging
import time

router = APIRouter()
logger = logging.getLogger(__name__)

# Simple in-memory rate limit: owner_id → last email timestamp
_qr_scan_email_sent: dict[str, float] = {}
_QR_EMAIL_COOLDOWN_SECONDS = 3600  # 1 hour per vendor


def _verify_url(audit_hash: str) -> str:
    return f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{audit_hash}"


async def _notify_owner_of_qr_scan(owner_id: str, company_name: str, owner_email: str) -> None:
    """Fire-and-forget: email the vendor when their QR badge is scanned."""
    now = time.time()
    if now - _qr_scan_email_sent.get(owner_id, 0) < _QR_EMAIL_COOLDOWN_SECONDS:
        return  # rate-limited
    _qr_scan_email_sent[owner_id] = now
    try:
        body_html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
          <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
            <h1 style="color:#10b981;margin:0;font-size:18px;">Your Profile Was Just Verified</h1>
          </div>
          <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
            <p>Hi {company_name},</p>
            <p>A buyer just scanned your <strong>BOOPPA verification QR badge</strong> and viewed your compliance evidence.</p>
            <p>This means a procurement team is actively evaluating you.</p>
            <p>
              <a href="https://www.booppa.io/vendor/dashboard"
                 style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                        border-radius:8px;font-weight:bold;display:inline-block;">
                View Your Dashboard →
              </a>
            </p>
            <p style="color:#64748b;font-size:12px;margin-top:24px;">booppa.io</p>
          </div>
        </body></html>
        """
        await EmailService().send_html_email(
            to_email=owner_email,
            subject="A buyer just verified your BOOPPA profile — BOOPPA",
            body_html=body_html,
        )
        logger.info(f"[Verify] QR scan email sent to {owner_email}")
    except Exception as e:
        logger.warning(f"[Verify] QR scan email failed for {owner_email}: {e}")


@router.get("/{audit_hash}")
async def verify_report(audit_hash: str):
    """Read-only verification endpoint for proof hashes."""
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.audit_hash == audit_hash).first()
        if not report:
            raise HTTPException(status_code=404, detail="Verification record not found")

        tx_hash = report.tx_hash
        anchored = False
        anchored_at = None
        tx_confirmed = None
        if tx_hash:
            blockchain = BlockchainService()
            status = blockchain.get_anchor_status(audit_hash, tx_hash=tx_hash)
            anchored = status.get("anchored", False)
            anchored_at = status.get("anchored_at")
            tx_confirmed = status.get("tx_confirmed")

        # Notify the vendor owner of the QR scan (fire-and-forget, rate-limited)
        try:
            owner = db.query(User).filter(User.id == report.owner_id).first()
            if owner and owner.email:
                asyncio.create_task(
                    _notify_owner_of_qr_scan(
                        str(report.owner_id),
                        report.company_name or owner.company or owner.email,
                        owner.email,
                    )
                )
        except Exception as e:
            logger.warning(f"[Verify] Could not schedule QR scan notification: {e}")

        return {
            "verify_id": audit_hash,
            "report_id": str(report.id),
            "framework": report.framework,
            "company_name": report.company_name,
            "status": report.status,
            "tx_hash": tx_hash,
            "anchored": anchored,
            "anchored_at": anchored_at,
            "tx_confirmed": tx_confirmed,
            "format": "BOOPPA-PROOF-SG",
            "schema_version": "1.0",
            "verify_url": f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{audit_hash}",
            "disclaimer": (
                "Verification is read-only and does not certify compliance or "
                "imply regulatory approval."
            ),
        }
    finally:
        db.close()
