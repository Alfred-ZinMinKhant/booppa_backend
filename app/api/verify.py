from fastapi import APIRouter, HTTPException
from app.core.db import SessionLocal
from app.core.models import Report
from app.core.config import settings
from app.services.blockchain import BlockchainService

router = APIRouter()


def _verify_url(audit_hash: str) -> str:
    return f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{audit_hash}"


@router.get("/{audit_hash}")
def verify_report(audit_hash: str):
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
