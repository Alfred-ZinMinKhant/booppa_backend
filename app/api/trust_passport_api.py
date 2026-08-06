"""
trust_passport_api.py — REST API for Trust Passport direct procurement system integration.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.vendor_features import _require_feature
from app.core.config import settings
from app.core.db import get_current_user, get_db
from app.core.models import ManagedEntity, Report, User

router = APIRouter(prefix="/passport", tags=["trust-passport-api"])


class PassportLookupResult(BaseModel):
    vendor_uen: str
    company_name: str
    passport_status: str
    audit_hash: Optional[str] = None
    verify_url: Optional[str] = None
    last_notarized_at: Optional[str] = None
    procurement_readiness: Optional[str] = None


class BatchLookupRequest(BaseModel):
    vendor_uens: List[str]


MAX_BATCH_SIZE = 200


@router.get("/reality-check")
async def get_reality_check(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns personalized compliance score benchmarks, sector rank, and 90-day action plan.
    Returns assessed: false if vendor score has not been calculated yet.
    """
    from app.core.models import VendorScore, VendorSector
    from app.services.my_reality_check_service import build_reality_check

    vendor_score = db.query(VendorScore).filter(VendorScore.user_id == current_user.id).first()
    if not vendor_score:
        return {
            "assessed": False,
            "message": "Complete the PDPA Quick Scan to unlock your personalized Reality Check.",
        }

    sector_row = db.query(VendorSector).filter(VendorSector.user_id == current_user.id).first()
    sector_name = sector_row.sector if sector_row else "General"

    sector_user_ids = [
        r.user_id for r in db.query(VendorSector).filter(VendorSector.sector == sector_name).all()
    ]
    sector_scores = [
        {
            "compliance_score": s.compliance_score or 0,
            "visibility_score": s.visibility_score or 0,
            "engagement_score": s.engagement_score or 0,
            "recency_score": s.recency_score or 0,
            "procurement_interest_score": s.procurement_interest_score or 0,
            "total_score": s.total_score or 0,
        }
        for s in db.query(VendorScore).filter(VendorScore.user_id.in_(sector_user_ids)).all()
    ]

    if not sector_scores:
        sector_scores = [{
            "compliance_score": vendor_score.compliance_score or 0,
            "visibility_score": vendor_score.visibility_score or 0,
            "engagement_score": vendor_score.engagement_score or 0,
            "recency_score": vendor_score.recency_score or 0,
            "procurement_interest_score": vendor_score.procurement_interest_score or 0,
            "total_score": vendor_score.total_score or 0,
        }]

    vendor_scores_dict = {
        "compliance_score": vendor_score.compliance_score or 0,
        "visibility_score": vendor_score.visibility_score or 0,
        "engagement_score": vendor_score.engagement_score or 0,
        "recency_score": vendor_score.recency_score or 0,
        "procurement_interest_score": vendor_score.procurement_interest_score or 0,
        "total_score": vendor_score.total_score or 0,
    }

    result = build_reality_check(str(current_user.id), sector_name, vendor_scores_dict, sector_scores)
    return {
        "assessed": True,
        "vendor_id": result.vendor_id,
        "vendor_total_score": result.vendor_total_score,
        "sector": result.sector,
        "sector_average": result.sector_average,
        "percentile_rank": result.percentile_rank,
        "dimension_scores": result.dimension_scores,
        "dimension_vs_sector": result.dimension_vs_sector,
        "top_gaps": result.top_gaps,
        "ninety_day_plan": result.ninety_day_plan,
    }


@router.get("/{vendor_uen}", response_model=PassportLookupResult)
async def get_passport_by_uen(
    vendor_uen: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "api_access", "Trust Passport API")

    entity = db.query(ManagedEntity).filter(ManagedEntity.uen == vendor_uen).first()
    if not entity:
        raise HTTPException(status_code=404, detail=f"No vendor found for UEN {vendor_uen}")

    report = (
        db.query(Report)
        .filter(Report.company_name == entity.company_name, Report.framework == "trust_passport")
        .order_by(Report.created_at.desc())
        .first()
    )

    if not report:
        return PassportLookupResult(
            vendor_uen=vendor_uen,
            company_name=entity.company_name,
            passport_status="not_found",
        )

    assessment = report.assessment_data or {}
    verify_base = getattr(settings, "VERIFY_BASE_URL", "https://booppa.io")
    return PassportLookupResult(
        vendor_uen=vendor_uen,
        company_name=entity.company_name,
        passport_status=assessment.get("tier", "L1"),
        audit_hash=report.audit_hash,
        verify_url=f"{verify_base}/verify/{report.audit_hash}" if report.audit_hash else None,
        last_notarized_at=report.created_at.isoformat() if report.created_at else None,
        procurement_readiness=assessment.get("procurement_readiness"),
    )


@router.post("/batch", response_model=List[PassportLookupResult])
async def get_passports_batch(
    body: BatchLookupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "api_access", "Trust Passport API")

    if len(body.vendor_uens) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=422,
            detail=f"Batch limited to {MAX_BATCH_SIZE} UENs per request, received {len(body.vendor_uens)}",
        )

    results = []
    verify_base = getattr(settings, "VERIFY_BASE_URL", "https://booppa.io")
    for uen in body.vendor_uens:
        entity = db.query(ManagedEntity).filter(ManagedEntity.uen == uen).first()
        if not entity:
            results.append(PassportLookupResult(vendor_uen=uen, company_name="", passport_status="not_found"))
            continue

        report = (
            db.query(Report)
            .filter(Report.company_name == entity.company_name, Report.framework == "trust_passport")
            .order_by(Report.created_at.desc())
            .first()
        )
        if not report:
            results.append(PassportLookupResult(vendor_uen=uen, company_name=entity.company_name, passport_status="not_found"))
            continue

        assessment = report.assessment_data or {}
        results.append(
            PassportLookupResult(
                vendor_uen=uen,
                company_name=entity.company_name,
                passport_status=assessment.get("tier", "L1"),
                audit_hash=report.audit_hash,
                verify_url=f"{verify_base}/verify/{report.audit_hash}" if report.audit_hash else None,
                last_notarized_at=report.created_at.isoformat() if report.created_at else None,
                procurement_readiness=assessment.get("procurement_readiness"),
            )
        )

    return results


