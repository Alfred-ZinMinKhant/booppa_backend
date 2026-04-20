"""
Vendor Comparison Service
=========================
Phase 2 feature: side-by-side vendor comparison matrix.
"""

import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.core.models import User
from app.core.models_v6 import VendorScore, VerifyRecord, VendorSector
from app.core.models_v8 import VendorStatusSnapshot, ScoreSnapshot
from app.core.models_v10 import MarketplaceVendor

logger = logging.getLogger(__name__)


def compare_vendors(db: Session, vendor_ids: list[str]) -> dict:
    """Generate comparison matrix for 2-4 vendors.

    vendor_ids are MarketplaceVendor UUIDs (marketplace_vendors.id).
    We join to users via claimed_by_user_id to fetch scores and status.
    """
    if len(vendor_ids) < 2 or len(vendor_ids) > 4:
        return {"error": "Provide 2-4 vendor IDs for comparison"}

    vendors = []
    for vid in vendor_ids:
        mv = db.query(MarketplaceVendor).filter(MarketplaceVendor.id == vid).first()
        if not mv:
            continue

        # Use the linked user for score/status lookups if available
        user_id = str(mv.claimed_by_user_id) if mv.claimed_by_user_id else None
        score = (
            db.query(VendorScore).filter(VendorScore.vendor_id == user_id).first()
            if user_id
            else None
        )
        status = (
            db.query(VendorStatusSnapshot)
            .filter(VendorStatusSnapshot.vendor_id == user_id)
            .first()
            if user_id
            else None
        )
        verify = (
            db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user_id).first()
            if user_id
            else None
        )
        sectors = (
            db.query(VendorSector).filter(VendorSector.vendor_id == user_id).all()
            if user_id
            else []
        )

        vendors.append(
            {
                "id": str(mv.id),
                "company": mv.company_name,
                "uen": mv.uen,
                "scores": (
                    {
                        "total_score": score.total_score if score else 0,
                        "compliance_score": score.compliance_score if score else 0,
                        "visibility_score": score.visibility_score if score else 0,
                        "engagement_score": score.engagement_score if score else 0,
                        "recency_score": score.recency_score if score else 0,
                        "procurement_interest_score": (
                            score.procurement_interest_score if score else 0
                        ),
                    }
                    if score
                    else None
                ),
                "status": {
                    "verification_depth": (
                        status.verification_depth if status else "UNVERIFIED"
                    ),
                    "monitoring_activity": (
                        status.monitoring_activity if status else "NONE"
                    ),
                    "risk_signal": status.risk_signal if status else "CLEAN",
                    "procurement_readiness": (
                        status.procurement_readiness if status else "NOT_READY"
                    ),
                    "confidence_score": status.confidence_score if status else 0,
                    "evidence_count": status.evidence_count if status else 0,
                    "notarization_depth": status.notarization_depth if status else 0,
                },
                "verification": (
                    {
                        "lifecycle_status": verify.lifecycle_status if verify else None,
                        "verified_at": (
                            verify.verified_at.isoformat()
                            if verify
                            and hasattr(verify, "verified_at")
                            and verify.verified_at
                            else None
                        ),
                    }
                    if verify
                    else None
                ),
                "sectors": [s.sector for s in sectors],
            }
        )

    # Build comparison dimensions
    dimensions = [
        "total_score",
        "compliance_score",
        "visibility_score",
        "verification_depth",
        "risk_signal",
        "procurement_readiness",
        "confidence_score",
    ]

    return {
        "vendors": vendors,
        "vendor_count": len(vendors),
        "dimensions": dimensions,
    }


def find_comparable_vendors(db: Session, vendor_id: str, limit: int = 5) -> list[dict]:
    """Find vendors in the same sector with similar scores for comparison."""
    vendor_sectors = (
        db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).all()
    )
    if not vendor_sectors:
        return []

    sector_names = [s.sector for s in vendor_sectors]
    vendor_score = (
        db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
    )
    target_score = vendor_score.total_score if vendor_score else 50

    # Find vendors in same sector with similar scores
    similar = (
        db.query(VendorScore)
        .join(VendorSector, VendorScore.vendor_id == VendorSector.vendor_id)
        .filter(
            VendorSector.sector.in_(sector_names),
            VendorScore.vendor_id != vendor_id,
        )
        .order_by(func.abs(VendorScore.total_score - target_score))
        .limit(limit)
        .all()
    )

    results = []
    for s in similar:
        user = db.query(User).filter(User.id == s.vendor_id).first()
        if user:
            results.append(
                {
                    "id": str(s.vendor_id),
                    "company": user.company or user.full_name,
                    "total_score": s.total_score,
                    "score_difference": abs(s.total_score - target_score),
                }
            )

    return results


# Import needed for find_comparable_vendors query
from sqlalchemy import func
