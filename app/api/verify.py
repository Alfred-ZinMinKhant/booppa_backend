from fastapi import APIRouter, HTTPException
from app.core.db import SessionLocal
from app.core.models import Report
from app.core.config import settings
from app.services.blockchain import BlockchainService

router = APIRouter()


def _verify_url(audit_hash: str) -> str:
    return f"{settings.VERIFY_BASE_URL.rstrip('/')}/{audit_hash}"


@router.get("/verify/{audit_hash}")
def verify_report(audit_hash: str):
    """Read-only verification endpoint for proof hashes."""
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.audit_hash == audit_hash).first()
        if not report:
            raise HTTPException(status_code=404, detail="Verification record not found")

        tx_hash = report.tx_hash
        anchored = False
        if tx_hash:
            blockchain = BlockchainService()
            anchored = blockchain.verify_anchored(audit_hash)

        return {
            "verify_id": audit_hash,
            "report_id": str(report.id),
            "framework": report.framework,
            "company_name": report.company_name,
            "status": report.status,
            "tx_hash": tx_hash,
            "anchored": anchored,
            "format": "BOOPPA-PROOF-SG",
            "schema_version": "1.0",
            "verify_url": _verify_url(audit_hash),
            "disclaimer": (
                "Verification is read-only and does not certify compliance or "
                "imply regulatory approval."
            ),
        }
    finally:
        db.close()
