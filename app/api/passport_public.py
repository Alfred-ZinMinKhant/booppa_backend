"""
passport_public.py — Public Trust Passport vendor profile route.

GET /passport/profile/{uen} — Public aggregated vendor profile route.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import ManagedEntity, Report

router = APIRouter(prefix="/passport", tags=["passport-public"])


@router.get("/profile/{uen}")
async def get_passport_profile(uen: str, db: Session = Depends(get_db)):
    """
    Public aggregated vendor profile for Trust Passport (by UEN).
    """
    entity = db.query(ManagedEntity).filter(ManagedEntity.uen == uen).first()
    if not entity:
        raise HTTPException(status_code=404, detail=f"Vendor with UEN {uen} not found")

    reports = (
        db.query(Report)
        .filter(
            Report.company_name == entity.company_name,
            Report.framework == "trust_passport",
        )
        .order_by(Report.created_at.desc())
        .all()
    )

    latest_report = reports[0] if reports else None
    assessment = latest_report.assessment_data if latest_report else {}

    history = [
        {
            "audit_hash": r.audit_hash,
            "status": r.status,
            "tier": (r.assessment_data or {}).get("tier"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reports
    ]

    return {
        "uen": uen,
        "company_name": entity.company_name,
        "legal_name": entity.legal_name,
        "registered_address": entity.registered_address,
        "latest_tier": assessment.get("tier", "L1"),
        "latest_audit_hash": latest_report.audit_hash if latest_report else None,
        "history": history,
    }
