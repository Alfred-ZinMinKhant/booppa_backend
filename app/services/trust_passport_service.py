"""
trust_passport_service.py — Trust Passport orchestration service (L1/L2/L3).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PassportTier(str, Enum):
    L1_AUTOMATED = "L1"
    L2_NOTARISED = "L2"
    L3_MONITORING = "L3"


@dataclass
class TrustPassportResult:
    vendor_id: str
    company_name: str
    tier: PassportTier
    audit_hash: Optional[str]
    verify_url: Optional[str]
    generated_at: str
    pdf_bytes: bytes
    source_data: dict


def generate_l1_passport(vendor_data: dict, verify_base_url: str) -> TrustPassportResult:
    company = vendor_data.get("company_name", "")

    return TrustPassportResult(
        vendor_id=vendor_data.get("vendor_id", ""),
        company_name=company,
        tier=PassportTier.L1_AUTOMATED,
        audit_hash=None,
        verify_url=None,
        generated_at=datetime.now(timezone.utc).isoformat(),
        pdf_bytes=b"",
        source_data=vendor_data,
    )


def assemble_l2_passport_data(deep_scan_result: dict, vendor_data: dict) -> dict:
    dimensions = deep_scan_result.get("dimensions", [])
    failing = [d for d in dimensions if d.get("status") not in ("PASS", "ok")]

    return {
        "company_name": vendor_data.get("company_name", ""),
        "vendor_id": vendor_data.get("vendor_id", ""),
        "tier": "L2",
        "dimensions": dimensions,
        "dimensions_total": len(dimensions),
        "dimensions_failing": len(failing),
        "certifications": deep_scan_result.get("certifications"),
        "financial_risk": deep_scan_result.get("financial_risk"),
        "scan_id": deep_scan_result.get("scan_id"),
        "captured_at": deep_scan_result.get("captured_at"),
    }


def create_passport_notarization_record(
    company_name: str, owner_id: str, passport_data: dict, content_hash: str
) -> dict:
    return {
        "framework": "trust_passport",
        "company_name": company_name,
        "owner_id": owner_id,
        "assessment_data": passport_data,
        "status": "pending",
        "audit_hash": content_hash,
    }


def get_trust_passport_history(db, vendor_id: str, limit: int = 24) -> list[dict]:
    from app.core.models import Report

    reports = (
        db.query(Report)
        .filter(Report.owner_id == vendor_id, Report.framework == "trust_passport")
        .order_by(Report.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "audit_hash": r.audit_hash,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "tier": (r.assessment_data or {}).get("tier"),
            "dimensions_failing": (r.assessment_data or {}).get("dimensions_failing"),
        }
        for r in reports
    ]

