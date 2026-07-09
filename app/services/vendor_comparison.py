"""
Vendor Comparison Service
=========================
Phase 2 feature: side-by-side vendor comparison matrix.
"""

import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.core.models import User
from app.core.models import VendorScore, VerifyRecord, VendorSector
from app.core.models import VendorStatusSnapshot, ScoreSnapshot
from app.core.models import MarketplaceVendor

logger = logging.getLogger(__name__)


def _framework_total(db, buyer_org_id, vendor_user_id, score) -> int:
    """Vendor total under the buyer's evaluation framework (read-time reweight).
    Falls back to the stored total when no org/framework or no score."""
    if not score:
        return 0
    if not buyer_org_id or not vendor_user_id:
        return score.total_score or 0
    try:
        from app.services.scoring import VendorScoreEngine
        weights = VendorScoreEngine.resolve_weights(db, buyer_org_id, vendor_user_id)
        return VendorScoreEngine.calculate_total({
            "complianceScore": score.compliance_score or 0,
            "visibilityScore": score.visibility_score or 0,
            "engagementScore": score.engagement_score or 0,
            "recencyScore": score.recency_score or 0,
            "procurementInterestScore": score.procurement_interest_score or 0,
        }, weights)
    except Exception:
        return score.total_score or 0


def compare_vendors(db: Session, vendor_ids: list[str], buyer_user=None) -> dict:
    """Generate comparison matrix for 2-4 vendors.

    vendor_ids are MarketplaceVendor UUIDs (marketplace_vendors.id).
    We join to users via claimed_by_user_id to fetch scores and status.

    When `buyer_user` is provided and their org has an active evaluation
    framework, each vendor's `total_score` is recomputed from its stored
    component scores under the framework's weights (read-time reweight).
    """
    if len(vendor_ids) < 2 or len(vendor_ids) > 4:
        return {"error": "Provide 2-4 vendor IDs for comparison"}

    # Resolve the buyer's org once for framework-weighted totals.
    buyer_org_id = None
    if buyer_user is not None:
        try:
            from app.core.models import Organisation
            org = db.query(Organisation).filter(
                Organisation.owner_user_id == buyer_user.id
            ).first()
            buyer_org_id = org.id if org else None
        except Exception:
            buyer_org_id = None

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
                        "total_score": _framework_total(db, buyer_org_id, user_id, score),
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
