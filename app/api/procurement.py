"""
Procurement Dashboard Routes — V8 — Enterprise Pro Layer
=========================================================
All routes require ENTERPRISE or ADMIN role.
Vendors NEVER see these endpoints.
No vendor is notified by any action performed here.

GET /api/procurement/vendors                     → ranked vendor list
GET /api/procurement/vendor/{slug}               → full vendor dossier
GET /api/procurement/sector/{sector}             → sector intelligence
GET /api/procurement/rfp-signals                 → active RFP clusters
GET /api/procurement/snapshot/{vendor_slug}      → audit-ready snapshot
GET /api/procurement/ordering-policy             → public transparency
GET /api/procurement/sector-percentiles/{sector} → percentile rankings
GET /api/procurement/vendor/{slug}/status        → VendorStatusProfile by slug
"""

import math
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.db import get_db, get_current_user
from app.core.models import (
    User, VendorScore, VerifyRecord, VendorSector,
    EnterpriseProfile, GovernanceRecord, ActivityLog,
)
from app.core.models_v8 import (
    VendorStatusSnapshot, ScoreSnapshot, NotarizationMetadata,
)
from app.services.vendor_status import get_vendor_status
from app.services.notarization_elevation import (
    fetch_elevation_metadata,
    fetch_elevation_metadata_batch,
)
from app.services.sector_pressure import get_sector_competitive_pressure

router = APIRouter()

ORDERING_POLICY = {
    "version":     "1.0",
    "description": (
        "Vendor ordering is based primarily on compliance-weighted score. "
        "ELEVATED vendors (via NotarizationElevationLayer) may receive a "
        "view-layer trust signal boost of up to 15 points in display only — "
        "this boost is never stored back to the database and does not affect "
        "the vendor's VendorTier, percentile, or ScoreSnapshot."
    ),
    "modifiers": [
        {"name": "score_first",       "maxBoost": 0,  "inputs": ["finalScore"]},
        {"name": "verified_first",    "maxBoost": 15, "inputs": ["verificationDepth"]},
        {"name": "snapshot_first",    "maxBoost": 10, "inputs": ["monitoringActivity"]},
        {"name": "composite",         "maxBoost": 15, "inputs": ["finalScore", "verificationDepth", "monitoringActivity", "confidenceScore"]},
    ],
}


def _require_enterprise(current_user):
    if getattr(current_user, "role", "VENDOR") not in ("ENTERPRISE", "ADMIN"):
        raise HTTPException(status_code=403, detail="Enterprise plan required.")


def _predict_downgrade_risk(risk_score: float, stability: float, volatility: float) -> dict:
    base = risk_score * 0.5 + (1 - stability) * 30 + min(20, volatility * 0.2)
    composite = min(100, base)
    if composite >= 75:
        return {"level": "CRITICAL", "score": round(composite), "reason": "High anomaly risk + low stability"}
    if composite >= 50:
        return {"level": "HIGH",     "score": round(composite), "reason": "Score volatility detected"}
    if composite >= 25:
        return {"level": "MEDIUM",   "score": round(composite), "reason": "Minor instability signals"}
    return          {"level": "LOW",      "score": round(composite), "reason": "Stable profile"}


@router.get("/vendors")
async def procurement_vendors(
    sector:      Optional[str]  = Query(None),
    min_score:   Optional[int]  = Query(None, ge=0, le=1000),
    verified:    bool           = Query(False),
    limit:       int            = Query(30, ge=1, le=100),
    page:        int            = Query(1, ge=1),
    order_by:    str            = Query("SCORE_FIRST"),
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Ranked vendor list with score breakdown, risk, and stability."""
    _require_enterprise(current_user)

    # Base score query
    query = db.query(VendorScore)
    if min_score is not None:
        query = query.filter(VendorScore.total_score >= min_score)
    if verified:
        query = query.join(
            VerifyRecord, VerifyRecord.vendor_id == VendorScore.vendor_id
        ).filter(VerifyRecord.lifecycle_status.in_(["ACTIVE"]))
    if sector:
        sector_ids = db.query(VendorSector.vendor_id).filter(
            VendorSector.sector == sector
        ).all()
        sector_ids = [s[0] for s in sector_ids]
        if not sector_ids:
            return {"vendors": [], "page": page, "limit": limit, "totalCount": 0, "orderBy": order_by}
        query = query.filter(VendorScore.vendor_id.in_(sector_ids))

    total_count  = query.count()
    score_rows   = query.order_by(VendorScore.total_score.desc()).offset((page - 1) * limit).limit(limit).all()

    vendor_ids = [str(s.vendor_id) for s in score_rows]
    elevation_map = fetch_elevation_metadata_batch(db, vendor_ids)

    # Pull status snapshots
    status_rows = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id.in_(vendor_ids)
    ).all()
    status_map = {str(s.vendor_id): s for s in status_rows}

    # Pull score history for trajectory/volatility
    snapshots_map: dict = {}
    for s in score_rows:
        snaps = db.query(ScoreSnapshot).filter(
            ScoreSnapshot.vendor_id == s.vendor_id
        ).order_by(ScoreSnapshot.snapshot_at.desc()).limit(10).all()
        snapshots_map[str(s.vendor_id)] = snaps

    # Pull user info (company, slug)
    user_map: dict = {}
    for s in score_rows:
        u = db.query(User).filter(User.id == s.vendor_id).first()
        if u:
            user_map[str(s.vendor_id)] = u

    # Enrich each vendor
    enriched = []
    for s in score_rows:
        vid        = str(s.vendor_id)
        user       = user_map.get(vid)
        snaps      = snapshots_map.get(vid, [])
        scores5    = [sn.final_score for sn in snaps[:5]]
        mean5      = sum(scores5) / len(scores5) if scores5 else s.total_score
        volatility = round(math.sqrt(sum((sc - mean5)**2 for sc in scores5) / max(len(scores5), 1))) if len(scores5) >= 2 else 0
        stability  = max(0.0, min(1.0, 1 - volatility / 200))

        trajectory = "INSUFFICIENT_DATA"
        if len(scores5) >= 2:
            trajectory = "RISING" if scores5[0] > scores5[-1] else ("FALLING" if scores5[0] < scores5[-1] else "STABLE")

        downgrade_risk = _predict_downgrade_risk(0.0, stability, volatility)

        nel        = elevation_map.get(vid, {})
        status_row = status_map.get(vid)

        pct  = snaps[0].sector_percentile if snaps else 50.0
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == s.vendor_id).first()

        enriched.append({
            "slug":               user.email.split("@")[0] if user else vid[:8],
            "company":            user.company if user else None,
            "currentScore":       s.total_score,
            "breakdown": {
                "compliance":    s.compliance_score,
                "visibility":    s.visibility_score,
                "engagement":    s.engagement_score,
                "recency":       s.recency_score,
                "procurement":   s.procurement_interest_score,
            },
            "verified":           verify is not None and verify.lifecycle_status.value == "ACTIVE",
            "complianceScore":    verify.compliance_score if verify else 0,
            "verifyExpiry":       verify.expires_at.isoformat() if verify and verify.expires_at else None,
            "stabilityIndex":     round(stability, 2),
            "volatility":         volatility,
            "trajectory":         trajectory,
            "downgradeRisk":      downgrade_risk,
            "sectorPercentile":   pct,
            "verificationDepth":  status_row.verification_depth if status_row else "UNVERIFIED",
            "monitoringActivity": status_row.monitoring_activity if status_row else "NONE",
            "riskSignal":         status_row.risk_signal if status_row else "CLEAN",
            "procurementReadiness": status_row.procurement_readiness if status_row else "NOT_READY",
            "elevation": {
                "structuralLevel":   nel.get("structural_level", "STANDARD"),
                "verificationDepth": nel.get("verification_depth"),
                "notarizedAt":       nel.get("notarized_at"),
                "validationId":      nel.get("validation_id"),
            },
        })

    # Simple ordering (pure, no DB)
    if order_by == "VERIFIED_FIRST":
        enriched.sort(key=lambda v: (v["elevation"]["structuralLevel"] == "ELEVATED", v["currentScore"]), reverse=True)
    elif order_by == "SNAPSHOT_FIRST":
        enriched.sort(key=lambda v: (v["monitoringActivity"] == "ACTIVE", v["currentScore"]), reverse=True)
    elif order_by == "COMPOSITE":
        enriched.sort(key=lambda v: (
            v["currentScore"]
            + (10 if v["elevation"]["structuralLevel"] == "ELEVATED" else 0)
            + (5 if v["monitoringActivity"] == "ACTIVE" else 0)
        ), reverse=True)
    # else SCORE_FIRST — already ordered by DB query

    return {"vendors": enriched, "page": page, "limit": limit, "totalCount": total_count, "orderBy": order_by}


@router.get("/vendor/{vendor_slug}/status")
async def procurement_vendor_status(
    vendor_slug: str,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Full VendorStatusProfile for a vendor identified by slug/email prefix."""
    _require_enterprise(current_user)
    user = db.query(User).filter(User.email.like(f"{vendor_slug}@%")).first()
    if not user:
        user = db.query(User).filter(User.company == vendor_slug).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found.")
    return get_vendor_status(db, str(user.id))


@router.get("/sector/{sector}")
async def procurement_sector(
    sector:      str,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Sector intelligence: vendor count, avg score, tier distribution, top vendors."""
    _require_enterprise(current_user)

    vendor_ids_in_sector = db.query(VendorSector.vendor_id).filter(
        VendorSector.sector == sector
    ).all()
    ids = [v[0] for v in vendor_ids_in_sector]
    vendor_count = len(ids)

    if not ids:
        return {"sector": sector, "vendorCount": 0}

    avg_score_row = db.query(func.avg(VendorScore.total_score)).filter(
        VendorScore.vendor_id.in_(ids)
    ).scalar()
    avg_score = round(avg_score_row or 0)

    verified_count = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id.in_(ids),
        VerifyRecord.lifecycle_status.in_(["ACTIVE"]),
    ).count()

    # Top 5 by score
    top_scores = db.query(VendorScore, User).join(
        User, User.id == VendorScore.vendor_id
    ).filter(VendorScore.vendor_id.in_(ids)).order_by(
        VendorScore.total_score.desc()
    ).limit(5).all()

    top_vendors = [
        {
            "rank":    i + 1,
            "company": u.company,
            "score":   vs.total_score,
        }
        for i, (vs, u) in enumerate(top_scores)
    ]

    # Elevated count
    elevated_count = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id.in_(ids),
        NotarizationMetadata.structural_level == "ELEVATED",
    ).count()

    return {
        "sector":        sector,
        "vendorCount":   vendor_count,
        "verifiedCount": verified_count,
        "verifiedPct":   round(verified_count / vendor_count * 100) if vendor_count else 0,
        "avgScore":      avg_score,
        "elevatedCount": elevated_count,
        "elevatedPct":   round(elevated_count / vendor_count * 100) if vendor_count else 0,
        "topVendors":    top_vendors,
    }


@router.get("/rfp-signals")
async def rfp_signals(
    sector:      Optional[str] = Query(None),
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Active high-intent enterprise clusters (procurement signals)."""
    _require_enterprise(current_user)

    query = db.query(EnterpriseProfile).filter(
        EnterpriseProfile.procurement_intent_score >= 61
    )
    profiles = query.order_by(EnterpriseProfile.procurement_intent_score.desc()).limit(50).all()

    signals = [
        {
            "domain":      p.domain,
            "orgType":     p.organization_type.value if p.organization_type else "UNKNOWN",
            "isGov":       p.is_government,
            "intentScore": p.procurement_intent_score,
            "isActiveRFP": p.active_procurement,
            "viewCount7d": p.visit_frequency,
            "lastSeenAt":  p.last_activity.isoformat() if p.last_activity else None,
        }
        for p in profiles
    ]

    return {"signals": signals, "count": len(signals), "asOf": datetime.utcnow().isoformat()}


@router.get("/snapshot/{vendor_slug}")
async def procurement_snapshot(
    vendor_slug: str,
    window:      int        = Query(30, ge=1, le=365),
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Institutional-grade, audit-ready vendor record."""
    _require_enterprise(current_user)

    user = db.query(User).filter(
        (User.company == vendor_slug) | (User.email.like(f"{vendor_slug}@%"))
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    vendor_id  = str(user.id)
    score_row  = db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
    verify     = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    elevation  = fetch_elevation_metadata(db, vendor_id)

    cutoff  = datetime.utcnow() - timedelta(days=window)
    proof_view_count = 0
    if verify:
        from app.core.models import ProofView
        proof_view_count = db.query(ProofView).filter(
            ProofView.verify_id == verify.id,
            ProofView.created_at >= cutoff,
        ).count()

    snapshot_hash = hashlib.sha256(
        json.dumps({
            "vendorId":    vendor_id,
            "score":       score_row.total_score if score_row else 0,
            "verified":    verify.lifecycle_status.value if verify else "NONE",
            "window":      window,
        }, sort_keys=True).encode()
    ).hexdigest()

    return {
        "vendor": {
            "company":         user.company,
            "email":           user.email,
        },
        "currentScore":        score_row.total_score if score_row else 0,
        "compliance": {
            "lifecycleStatus": verify.lifecycle_status.value if verify else "NONE",
            "complianceScore": verify.compliance_score if verify else 0,
            "expiresAt":       verify.expires_at.isoformat() if verify and verify.expires_at else None,
        },
        "elevation": {
            "structuralLevel":   elevation.get("structural_level"),
            "validationId":      elevation.get("validation_id"),
            "publicHash":        elevation.get("public_hash"),
            "confidenceScore":   elevation.get("confidence_score"),
        },
        "activityWindow": {
            "days":             window,
            "proofViewsInWindow": proof_view_count,
        },
        "snapshotHash":       snapshot_hash,
        "generatedAt":        datetime.utcnow().isoformat(),
    }


@router.get("/ordering-policy")
async def ordering_policy():
    """Public transparency endpoint — no auth required."""
    return {
        "policy":        ORDERING_POLICY,
        "retrievedAt":   datetime.utcnow().isoformat(),
        "documentation": "https://docs.booppa.com/procurement/ordering-policy",
    }


@router.get("/sector-percentiles/{sector}")
async def sector_percentiles(
    sector:      str,
    order_by:    str = Query("percentile"),
    limit:       int = Query(50, ge=1, le=100),
    page:        int = Query(1, ge=1),
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Risk-adjusted percentile rankings for all vendors in a sector. No raw scores."""
    _require_enterprise(current_user)

    ids = [v[0] for v in db.query(VendorSector.vendor_id).filter(
        VendorSector.sector == sector
    ).all()]

    if not ids:
        return {"sector": sector, "vendors": [], "page": page, "limit": limit, "totalCount": 0}

    status_rows = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id.in_(ids)
    ).all()

    rows_data = [
        {
            "vendorId":           str(s.vendor_id),
            "verificationDepth":  s.verification_depth,
            "monitoringActivity": s.monitoring_activity,
            "riskSignal":         s.risk_signal,
            "riskAdjustedPct":    s.risk_adjusted_pct,
            "dualSilentMode":     s.dual_silent_mode,
            "confidenceScore":    s.confidence_score,
        }
        for s in status_rows
    ]

    # Sort
    depth_rank = {"UNVERIFIED": 0, "BASIC": 1, "STANDARD": 2, "DEEP": 3, "CERTIFIED": 4}
    if order_by == "verificationDepth":
        rows_data.sort(key=lambda v: depth_rank.get(v["verificationDepth"], 0), reverse=True)
    elif order_by == "composite":
        rows_data.sort(key=lambda v: (
            v["riskAdjustedPct"]
            + depth_rank.get(v["verificationDepth"], 0) * 5
            + v["confidenceScore"] * 0.1
        ), reverse=True)
    else:  # percentile
        rows_data.sort(key=lambda v: v["riskAdjustedPct"], reverse=True)

    total_count = len(rows_data)
    paginated   = rows_data[(page - 1) * limit: page * limit]

    return {
        "sector":     sector,
        "vendors":    paginated,
        "page":       page,
        "limit":      limit,
        "totalCount": total_count,
    }
