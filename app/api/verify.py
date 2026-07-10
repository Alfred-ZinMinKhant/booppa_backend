from app.core.route_classes import RetryAPIRoute
from fastapi import APIRouter, HTTPException
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.core.config import settings
from app.services.blockchain import BlockchainService
from app.services.email_service import EmailService
import asyncio
import logging
import time
import uuid

router = APIRouter(route_class=RetryAPIRoute)
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
        from app.services.email_layout import branded_email_html, email_button
        body_html = branded_email_html(
            f"""
            <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Profile Was Just Verified</h2>
            <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hi {company_name},</p>
            <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">A buyer just scanned your <strong>BOOPPA verification QR badge</strong> and viewed your compliance evidence.</p>
            <p style="margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;">This means a procurement team is actively evaluating you.</p>
            {email_button("https://www.booppa.io/vendor/dashboard", "View Your Dashboard →")}
            <p style="margin:8px 0 0;color:#64748b;font-size:12px;">booppa.io</p>
            """,
            title="Your profile was just verified",
            preheader="A buyer just scanned your BOOPPA verification badge.",
        )
        await EmailService().send_html_email(
            to_email=owner_email,
            subject="A buyer just verified your BOOPPA profile — BOOPPA",
            body_html=body_html,
        )
        logger.info(f"[Verify] QR scan email sent to {owner_email}")
    except Exception as e:
        logger.warning(f"[Verify] QR scan email failed for {owner_email}: {e}")


@router.get("/cover-sheet/{report_id}")
async def verify_cover_sheet_by_report_id(report_id: str):
    """
    Public verification endpoint keyed by Report UUID.

    Used by the QR code printed on page 1 of every Compliance Cover Sheet —
    the QR encodes booppa.io/verify/<report_id> and resolves here so a
    procurement officer can scan with their phone and see a single
    pass/fail card without typing the 64-char SHA-256.

    Returns only public-facing fields (company, framework, issued date,
    tx hash, anchor status). Same shape as the audit-hash endpoint below
    for API consistency. The report_id is UUID4 (128 bits unguessable),
    so this is not enumerable.
    """
    db = SessionLocal()
    try:
        try:
            uuid.UUID(report_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid report id")

        from app.core.repositories.report_repository import ReportRepository
        report = ReportRepository.get_by_id(db, report_id)
        if not report:
            raise HTTPException(status_code=404, detail="Cover Sheet not found")

        tx_hash = report.tx_hash
        anchored = False
        anchored_at = None
        tx_confirmed = None
        if tx_hash:
            blockchain = BlockchainService()
            status = await blockchain.get_anchor_status(report.audit_hash or "", tx_hash=tx_hash)
            anchored = status.get("anchored", False)
            anchored_at = status.get("anchored_at")
            tx_confirmed = status.get("tx_confirmed")

        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        return {
            "verify_id": str(report.id),
            "report_id": str(report.id),
            "framework": report.framework,
            "company_name": report.company_name,
            "status": report.status,
            "tx_hash": tx_hash,
            "tx_network": "Polygon Amoy Testnet",
            "audit_hash": report.audit_hash,
            "anchored": anchored,
            "anchored_at": anchored_at,
            "tx_confirmed": tx_confirmed,
            "issued_at": report.completed_at.isoformat() if report.completed_at else (
                report.created_at.isoformat() if report.created_at else None
            ),
            "schema_version": ad.get("schema_version"),
            "format": "BOOPPA-PROOF-SG",
            "verify_url": f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{report.id}",
            "disclaimer": (
                "Verification is read-only and does not certify compliance or "
                "imply regulatory approval. Anchor confirms document existence "
                "at the timestamp above; not a guarantee of contents."
            ),
        }
    finally:
        db.close()


@router.get("/{audit_hash}")
async def verify_report(audit_hash: str):
    """Read-only verification endpoint for proof hashes."""
    db = SessionLocal()
    try:
        from app.core.repositories.report_repository import ReportRepository
        report = ReportRepository.get_by_audit_hash(db, audit_hash)
        if not report:
            raise HTTPException(status_code=404, detail="Verification record not found")

        tx_hash = report.tx_hash
        anchored = False
        anchored_at = None
        tx_confirmed = None
        if tx_hash:
            blockchain = BlockchainService()
            status = await blockchain.get_anchor_status(audit_hash, tx_hash=tx_hash)
            anchored = status.get("anchored", False)
            anchored_at = status.get("anchored_at")
            tx_confirmed = status.get("tx_confirmed")

        # Notify the vendor owner of the QR scan (fire-and-forget, rate-limited)
        try:
            from app.core.repositories.user_repository import UserRepository
            owner = UserRepository.get_by_id(db, str(report.owner_id))
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

        resp = {
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

        # Vendor Proof enrichment (additive) — a procurement officer scanning the
        # QR needs the ACRA standing, compliance score, and certificate validity,
        # not just document metadata. Only attached for vendor_proof records.
        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        if report.framework == "vendor_proof" or ad.get("vendor_proof_fulfilled"):
            expires_at = ad.get("certificate_expires_at")
            expired = None
            if expires_at:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    expired = _dt.fromisoformat(expires_at) < _dt.now(_tz.utc)
                except Exception:
                    expired = None
            resp["vendor_proof"] = {
                "compliance_score": ad.get("compliance_score"),
                "procurement_readiness": ad.get("procurement_readiness"),
                "verification_level": ad.get("verification_level") or "BASIC",
                "acra": {
                    "verified": ad.get("acra_verified", False),
                    "entity_type": ad.get("acra_entity_type"),
                    "registration_date": ad.get("acra_registration_date"),
                    "entity_status": ad.get("acra_entity_status"),
                    "entity_live": ad.get("acra_entity_live"),
                },
                "validity": {
                    "expires_at": expires_at,
                    "expired": expired,
                },
            }
        return resp
    finally:
        db.close()
