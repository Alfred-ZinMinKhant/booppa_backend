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
GET /api/procurement/export/csv                  → CSV export of vendor audit trail
GET /api/procurement/export/pdf                  → PDF export of vendor audit trail
"""

import csv
import io
import math
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.db import get_db, get_current_user
from app.core.models import (
    User, VendorScore, VerifyRecord, VendorSector,
    EnterpriseProfile, GovernanceRecord, ActivityLog,
)
from app.core.models_v10 import MarketplaceVendor
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


from app.billing.enforcement import PROCUREMENT_PLAN_KEYS
from app.billing.scan_credits import consume_scan, scan_usage


def _require_procurement(current_user):
    role = getattr(current_user, "role", "VENDOR")
    if role not in ("ADMIN", "PROCUREMENT"):
        raise HTTPException(status_code=403, detail="Procurement account required.")
    if role == "ADMIN":
        return  # admins always pass
    plan = (getattr(current_user, "plan", "free") or "free").lower().strip()
    if plan not in PROCUREMENT_PLAN_KEYS:
        raise HTTPException(
            status_code=403,
            detail="Procurement plan required. Subscribe to a Buyer or Suite tier to access procurement tools.",
        )


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


def _buyer_has_active_framework(db, current_user) -> bool:
    """Cheap check: does the buyer have a non-DEFAULT scoring framework in play?
    Lets the /vendors fast path stay pure-SQL when no framework is configured."""
    from app.core.models_enterprise import Organisation
    from app.core.models_v12 import VendorEvaluationFramework

    org = db.query(Organisation).filter(Organisation.owner_user_id == current_user.id).first()
    if not org:
        return False
    has_sector = (
        db.query(VendorEvaluationFramework.id)
        .filter(
            VendorEvaluationFramework.organisation_id == org.id,
            VendorEvaluationFramework.sector.isnot(None),
        ).first() is not None
    )
    if has_sector:
        return True
    if org.active_framework_id:
        active = (
            db.query(VendorEvaluationFramework)
            .filter(VendorEvaluationFramework.id == org.active_framework_id).first()
        )
        return bool(active and active.framework_type != "DEFAULT")
    return False


def _buyer_weight_map(db, current_user, vendor_ids):
    """Per-vendor scoring weights for the buyer's active evaluation framework.

    Returns None when the buyer has only the DEFAULT framework (or none) — the
    caller then keeps the fast stored-total SQL path. Otherwise returns a
    {vendor_id_str: weights_dict} map so the caller can re-rank from the stored
    component scores. Bounded to ~3 queries regardless of vendor count.
    """
    from app.core.models_enterprise import Organisation
    from app.core.models_v12 import VendorEvaluationFramework
    from app.services.scoring import VendorScoreEngine

    org = db.query(Organisation).filter(Organisation.owner_user_id == current_user.id).first()
    if not org:
        return None
    fws = (
        db.query(VendorEvaluationFramework)
        .filter(VendorEvaluationFramework.organisation_id == org.id)
        .all()
    )
    sector_fws = {f.sector.lower(): f.weights() for f in fws if f.sector}
    active = next((f for f in fws if str(f.id) == str(org.active_framework_id)), None) if org.active_framework_id else None
    default_w = active.weights() if active else VendorScoreEngine.WEIGHTS
    # Nothing custom in play → let the caller use the stored total.
    if not sector_fws and (active is None or active.framework_type == "DEFAULT"):
        return None

    # Bulk-load sectors for all candidate vendors (one query).
    sector_by_vendor: dict[str, list[str]] = {}
    if sector_fws and vendor_ids:
        for vid, sec in (
            db.query(VendorSector.vendor_id, VendorSector.sector)
            .filter(VendorSector.vendor_id.in_(vendor_ids)).all()
        ):
            sector_by_vendor.setdefault(str(vid), []).append((sec or "").lower())

    weight_map: dict[str, dict] = {}
    for vid in vendor_ids:
        vid = str(vid)
        chosen = default_w
        for sec in sector_by_vendor.get(vid, []):
            if sec in sector_fws:
                chosen = sector_fws[sec]
                break
        weight_map[vid] = chosen
    return weight_map


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
    """Ranked vendor list with score breakdown, risk, and stability.
    Includes ALL registered VENDOR-role users — even those without a score yet.
    """
    _require_procurement(current_user)

    # Base: all active VENDOR-role users, outer-joined to VendorScore so
    # vendors who haven't been scored yet still appear (score = 0).
    base = (
        db.query(User, VendorScore)
        .outerjoin(VendorScore, VendorScore.vendor_id == User.id)
        .filter(User.role == "VENDOR", User.is_active == True)
    )

    if min_score is not None:
        base = base.filter(
            func.coalesce(VendorScore.total_score, 0) >= min_score
        )
    if verified:
        verified_ids = db.query(VerifyRecord.vendor_id).filter(
            VerifyRecord.lifecycle_status.in_(["ACTIVE"])
        ).subquery()
        base = base.filter(User.id.in_(verified_ids))
    if sector:
        sector_ids = [
            s[0] for s in db.query(VendorSector.vendor_id)
            .filter(VendorSector.sector == sector).all()
        ]
        if not sector_ids:
            return {"vendors": [], "page": page, "limit": limit, "totalCount": 0, "orderBy": order_by}
        base = base.filter(User.id.in_(sector_ids))

    total_count = base.count()
    _offset = (page - 1) * limit

    if not _buyer_has_active_framework(db, current_user):
        # Fast path (unchanged): rank by stored total, paginate in SQL.
        rows = (
            base
            .order_by(func.coalesce(VendorScore.total_score, 0).desc())
            .offset(_offset)
            .limit(limit)
            .all()
        )
    else:
        # Framework path: recompute each vendor's total from stored components
        # using the buyer's active/sector weights, then rank + paginate in memory.
        from app.services.scoring import VendorScoreEngine

        _CANDIDATE_CAP = 1000
        candidates = (
            base.order_by(func.coalesce(VendorScore.total_score, 0).desc())
            .limit(_CANDIDATE_CAP).all()
        )
        weight_map = _buyer_weight_map(db, current_user, [u.id for u, _ in candidates]) or {}

        def _fw_total(pair):
            u, s = pair
            if not s:
                return 0
            return VendorScoreEngine.calculate_total({
                "complianceScore": s.compliance_score or 0,
                "visibilityScore": s.visibility_score or 0,
                "engagementScore": s.engagement_score or 0,
                "recencyScore": s.recency_score or 0,
                "procurementInterestScore": s.procurement_interest_score or 0,
            }, weight_map.get(str(u.id)))

        candidates.sort(key=_fw_total, reverse=True)
        rows = candidates[_offset:_offset + limit]

    vendor_ids = [str(user.id) for user, _ in rows]
    elevation_map = fetch_elevation_metadata_batch(db, vendor_ids)

    status_rows = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id.in_(vendor_ids)
    ).all()
    status_map = {str(s.vendor_id): s for s in status_rows}

    snapshots_map: dict = {}
    for user, _ in rows:
        snaps = db.query(ScoreSnapshot).filter(
            ScoreSnapshot.vendor_id == user.id
        ).order_by(ScoreSnapshot.snapshot_at.desc()).limit(10).all()
        snapshots_map[str(user.id)] = snaps

    mv_map: dict = {}
    for user, _ in rows:
        mv = db.query(MarketplaceVendor).filter(
            MarketplaceVendor.claimed_by_user_id == user.id
        ).first()
        if mv:
            mv_map[str(user.id)] = mv

    enriched = []
    for user, score_row in rows:
        vid        = str(user.id)
        s          = score_row  # may be None for unscored vendors
        snaps      = snapshots_map.get(vid, [])
        scores5    = [sn.final_score for sn in snaps[:5]]
        base_score = s.total_score if s else 0
        mean5      = sum(scores5) / len(scores5) if scores5 else base_score
        volatility = round(math.sqrt(sum((sc - mean5)**2 for sc in scores5) / max(len(scores5), 1))) if len(scores5) >= 2 else 0
        stability  = max(0.0, min(1.0, 1 - volatility / 200))

        trajectory = "INSUFFICIENT_DATA"
        if len(scores5) >= 2:
            trajectory = "RISING" if scores5[0] > scores5[-1] else ("FALLING" if scores5[0] < scores5[-1] else "STABLE")

        downgrade_risk = _predict_downgrade_risk(0.0, stability, volatility)

        nel        = elevation_map.get(vid, {})
        status_row = status_map.get(vid)

        pct    = snaps[0].sector_percentile if snaps else 50.0
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
        mv     = mv_map.get(vid)

        enriched.append({
            "slug":               user.email.split("@")[0],
            "company":            user.company or None,
            "website":            (mv.website if mv else None) or getattr(user, "website", None),
            "contactEmail":       (mv.contact_email if mv else None) or user.email,
            "domain":             mv.domain if mv else None,
            "currentScore":       base_score,
            "breakdown": {
                "compliance":    s.compliance_score if s else 0,
                "visibility":    s.visibility_score if s else 0,
                "engagement":    s.engagement_score if s else 0,
                "recency":       s.recency_score if s else 0,
                "procurement":   s.procurement_interest_score if s else 0,
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
    """Full VendorStatusProfile for a vendor identified by slug/email prefix.

    Consumes one QUICK scan credit per unique vendor per month (re-views free).
    """
    _require_procurement(current_user)
    user = db.query(User).filter(User.email.like(f"{vendor_slug}@%")).first()
    if not user:
        user = db.query(User).filter(User.company == vendor_slug).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    # Enforce quota BEFORE returning data — admins skip the meter.
    if getattr(current_user, "role", "") != "ADMIN":
        plan = (getattr(current_user, "plan", "free") or "free").lower().strip()
        consume_scan(db, current_user.id, plan, user.id, "QUICK")
        db.commit()

    return get_vendor_status(db, str(user.id))


@router.get("/sector/{sector}")
async def procurement_sector(
    sector:      str,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Sector intelligence: vendor count, avg score, tier distribution, top vendors."""
    _require_procurement(current_user)

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
    _require_procurement(current_user)

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

    return {"signals": signals, "count": len(signals), "asOf": datetime.now(timezone.utc).isoformat()}


@router.get("/snapshot/{vendor_slug}")
async def procurement_snapshot(
    vendor_slug: str,
    window:      int        = Query(30, ge=1, le=365),
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Institutional-grade, audit-ready vendor record.

    Consumes one DEEP scan credit per unique vendor per month (re-views free).
    Buyer Starter does not include DEEP scans — endpoint returns 402.
    """
    _require_procurement(current_user)

    user = db.query(User).filter(
        (User.company == vendor_slug) | (User.email.like(f"{vendor_slug}@%"))
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    if getattr(current_user, "role", "") != "ADMIN":
        plan = (getattr(current_user, "plan", "free") or "free").lower().strip()
        consume_scan(db, current_user.id, plan, user.id, "DEEP")
        db.commit()

    vendor_id  = str(user.id)
    score_row  = db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
    verify     = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    elevation  = fetch_elevation_metadata(db, vendor_id)

    cutoff  = datetime.now(timezone.utc) - timedelta(days=window)
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
        "generatedAt":        datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ordering-policy")
async def ordering_policy():
    """Public transparency endpoint — no auth required."""
    return {
        "policy":        ORDERING_POLICY,
        "retrievedAt":   datetime.now(timezone.utc).isoformat(),
        "documentation": "https://docs.booppa.com/procurement/ordering-policy",
    }


@router.get("/scan-quota")
async def procurement_scan_quota(
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Current month's scan usage per tier — feeds the dashboard widget.

    Response: { month, plan, scans: { QUICK: {used, limit, remaining}, ... } }
    `limit: null` = unlimited.
    """
    _require_procurement(current_user)
    plan = (getattr(current_user, "plan", "free") or "free").lower().strip()
    return scan_usage(db, current_user.id, plan)


@router.get("/vendor/{vendor_slug}/evidence")
async def procurement_vendor_evidence(
    vendor_slug: str,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Evidence Scan (L3) — blockchain evidence retrieval + complete dossier.

    Consumes one EVIDENCE credit per unique vendor per month (re-views free).
    Only Buyer Enterprise + Pro Suite + legacy Enterprise Pro include this tier.

    Aggregates:
      - All notarization tx_hashes (Polygon anchors) from Report
      - NotarizationMetadata (validation_id, public_hash, depth, confidence)
      - VerifyRecord lifecycle + expiry
      - CertificateLog entries (audit trail of all PDFs ever generated)
      - Recent ComplianceDriftEvent rows (last 10)
      - Snapshot-style headline fields (matches DEEP for continuity)
    """
    _require_procurement(current_user)

    user = db.query(User).filter(
        (User.company == vendor_slug) | (User.email.like(f"{vendor_slug}@%"))
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    if getattr(current_user, "role", "") != "ADMIN":
        plan = (getattr(current_user, "plan", "free") or "free").lower().strip()
        consume_scan(db, current_user.id, plan, user.id, "EVIDENCE")
        db.commit()

    from app.core.models import Report
    from app.core.models_v10 import CertificateLog
    from app.core.models_v8 import NotarizationMetadata, ComplianceDriftEvent

    # ── Blockchain anchors (every Report with a tx_hash) ─────────────────────
    anchored_reports = (
        db.query(Report)
        .filter(Report.owner_id == user.id, Report.tx_hash.isnot(None))
        .order_by(Report.completed_at.desc().nullslast(), Report.created_at.desc())
        .limit(50)
        .all()
    )
    anchors = [
        {
            "reportId":   str(r.id),
            "framework":  r.framework,
            "txHash":     r.tx_hash,
            "auditHash":  r.audit_hash,
            "anchoredAt": (r.completed_at or r.created_at).isoformat() if (r.completed_at or r.created_at) else None,
            "explorerUrl": f"https://polygonscan.com/tx/{r.tx_hash}" if r.tx_hash else None,
        }
        for r in anchored_reports
    ]

    # ── Notarization metadata (single row per vendor) ────────────────────────
    notmeta = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id == user.id
    ).first()
    notarization = (
        {
            "structuralLevel":   notmeta.structural_level,
            "verificationDepth": notmeta.verification_depth,
            "validationId":      notmeta.validation_id,
            "publicHash":        notmeta.public_hash,
            "evidenceCount":     notmeta.evidence_count,
            "confidenceScore":   notmeta.confidence_score,
            "notarizedAt":       notmeta.notarized_at.isoformat() if notmeta.notarized_at else None,
        }
        if notmeta
        else None
    )

    # ── Verify record (lifecycle, expiry) ────────────────────────────────────
    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    verify_record = (
        {
            "lifecycleStatus":     verify.lifecycle_status.value if verify.lifecycle_status else "NONE",
            "verificationLevel":   verify.verification_level.value if verify.verification_level else "BASIC",
            "complianceScore":     verify.compliance_score,
            "expiresAt":           verify.expires_at.isoformat() if verify.expires_at else None,
            "lastRefreshedAt":     verify.last_refreshed_at.isoformat() if verify.last_refreshed_at else None,
        }
        if verify
        else None
    )

    # ── Certificate log (audit trail of every PDF generated) ─────────────────
    certs = (
        db.query(CertificateLog)
        .filter(CertificateLog.vendor_id == user.id)
        .order_by(CertificateLog.generated_at.desc())
        .limit(50)
        .all()
    )
    certificate_log = [
        {
            "certificateType":  c.certificate_type,
            "fileHash":         c.file_hash,
            "generatedAt":      c.generated_at.isoformat() if c.generated_at else None,
            "downloadCount":    c.download_count,
            "lastDownloadedAt": c.downloaded_at.isoformat() if c.downloaded_at else None,
        }
        for c in certs
    ]

    # ── Compliance drift (last 10) ───────────────────────────────────────────
    drifts = (
        db.query(ComplianceDriftEvent)
        .filter(ComplianceDriftEvent.vendor_id == user.id)
        .order_by(ComplianceDriftEvent.created_at.desc())
        .limit(10)
        .all()
    )
    drift_history = [
        {
            "framework":     d.framework,
            "severity":      d.severity,
            "previousScore": d.previous_score,
            "currentScore":  d.current_score,
            "deltaPct":      d.delta_pct,
            "occurredAt":    d.created_at.isoformat() if d.created_at else None,
        }
        for d in drifts
    ]

    # ── Score headline (same field as DEEP for continuity) ───────────────────
    score_row = db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
    elevation = fetch_elevation_metadata(db, str(user.id))

    return {
        "vendor": {
            "company": user.company,
            "email":   user.email,
        },
        "currentScore":     score_row.total_score if score_row else 0,
        "verify":           verify_record,
        "notarization":     notarization,
        "elevation": {
            "structuralLevel": elevation.get("structural_level"),
            "validationId":    elevation.get("validation_id"),
            "publicHash":      elevation.get("public_hash"),
            "confidenceScore": elevation.get("confidence_score"),
        },
        "blockchainAnchors": anchors,
        "certificateLog":    certificate_log,
        "driftHistory":      drift_history,
        "generatedAt":       datetime.now(timezone.utc).isoformat(),
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
    _require_procurement(current_user)

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


# ── Audit Trail Export ───────────────────────────────────────────────────────

def _build_export_rows(db: Session, current_user) -> list[dict]:
    """Build a flat list of vendor records for CSV/PDF export."""
    scores = db.query(VendorScore).order_by(VendorScore.total_score.desc()).limit(500).all()
    rows = []
    for s in scores:
        vid = str(s.vendor_id)
        user = db.query(User).filter(User.id == s.vendor_id).first()
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == s.vendor_id).first()
        status = db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id == s.vendor_id).first()
        mv = db.query(MarketplaceVendor).filter(MarketplaceVendor.claimed_by_user_id == s.vendor_id).first()
        rows.append({
            "Company":              user.company if user else "",
            "Email":                user.email if user else "",
            "Website":              (mv.website if mv else None) or (user.website if user else "") or "",
            "Total Score":          s.total_score,
            "Compliance Score":     s.compliance_score,
            "Visibility Score":     s.visibility_score,
            "Engagement Score":     s.engagement_score,
            "Recency Score":        s.recency_score,
            "Verified":             "Yes" if (verify and verify.lifecycle_status.value == "ACTIVE") else "No",
            "Compliance Health":    verify.compliance_score if verify else 0,
            "Verify Expiry":        verify.expires_at.isoformat() if verify and verify.expires_at else "",
            "Verification Depth":   status.verification_depth if status else "UNVERIFIED",
            "Monitoring Activity":  status.monitoring_activity if status else "NONE",
            "Risk Signal":          status.risk_signal if status else "CLEAN",
            "Procurement Readiness": status.procurement_readiness if status else "NOT_READY",
        })
    return rows


@router.get("/export/csv")
async def export_csv(
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Download vendor audit trail as CSV."""
    _require_procurement(current_user)
    rows = _build_export_rows(db, current_user)
    if not rows:
        raise HTTPException(status_code=404, detail="No vendor data to export.")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=booppa_audit_trail_{ts}.csv"},
    )


@router.get("/export/pdf")
async def export_pdf(
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Download vendor audit trail as PDF."""
    _require_procurement(current_user)
    rows = _build_export_rows(db, current_user)
    if not rows:
        raise HTTPException(status_code=404, detail="No vendor data to export.")

    ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ts_file  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Build PDF using reportlab
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        raise HTTPException(status_code=501, detail="PDF export requires reportlab. Install with: pip install reportlab")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    elements.append(Paragraph(f"BOOPPA — Vendor Audit Trail Export", styles["Title"]))
    elements.append(Paragraph(f"Generated: {ts_label} | By: {current_user.email}", styles["Normal"]))
    elements.append(Spacer(1, 12))

    # Summary row
    total = len(rows)
    verified_count = sum(1 for r in rows if r["Verified"] == "Yes")
    avg_score = round(sum(r["Total Score"] for r in rows) / total) if total else 0
    elements.append(Paragraph(
        f"Total Vendors: {total} | Verified: {verified_count} | Avg Score: {avg_score}/100",
        styles["Normal"],
    ))
    elements.append(Spacer(1, 12))

    # Table — pick key columns to fit on landscape A4
    col_keys = ["Company", "Total Score", "Compliance Health", "Verified",
                "Verification Depth", "Risk Signal", "Procurement Readiness"]
    header = col_keys
    data = [header]
    for r in rows[:200]:  # cap at 200 rows for PDF readability
        data.append([str(r.get(k, "")) for k in col_keys])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("FONTSIZE",    (0, 1), (-1, -1), 7),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)

    if len(rows) > 200:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph(f"Showing 200 of {len(rows)} vendors. Use CSV export for full data.", styles["Normal"]))

    # Footer
    elements.append(Spacer(1, 20))
    snap_hash = hashlib.sha256(json.dumps([r["Company"] for r in rows], sort_keys=True).encode()).hexdigest()[:16]
    elements.append(Paragraph(f"Snapshot hash: {snap_hash} | booppa.io", styles["Normal"]))

    doc.build(elements)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=booppa_audit_trail_{ts_file}.pdf"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Vendor evaluation frameworks (scoring weight profiles)
# Powers Buyer Professional "customisable risk-scoring weights" and Buyer
# Enterprise "custom evaluation frameworks (MAS TRM / MOH)".
# ─────────────────────────────────────────────────────────────────────────────

# Built-in templates seeded lazily per org. Weights sum to 1.0.
_BUILTIN_FRAMEWORKS = {
    "DEFAULT": {
        "name": "Balanced (default)",
        "sector": None,
        "weights": {"COMPLIANCE": 0.30, "VISIBILITY": 0.20, "ENGAGEMENT": 0.20, "RECENCY": 0.15, "PROCUREMENT_INTEREST": 0.15},
    },
    "MAS_TRM": {
        "name": "MAS TRM (fintech)",
        "sector": "fintech",
        "weights": {"COMPLIANCE": 0.45, "VISIBILITY": 0.15, "ENGAGEMENT": 0.15, "RECENCY": 0.15, "PROCUREMENT_INTEREST": 0.10},
    },
    "MOH": {
        "name": "MOH (healthcare)",
        "sector": "healthcare",
        "weights": {"COMPLIANCE": 0.40, "VISIBILITY": 0.15, "ENGAGEMENT": 0.10, "RECENCY": 0.25, "PROCUREMENT_INTEREST": 0.10},
    },
}

_WEIGHT_KEYS = ("COMPLIANCE", "VISIBILITY", "ENGAGEMENT", "RECENCY", "PROCUREMENT_INTEREST")


def _ensure_builtin_frameworks(db: Session, org_id) -> None:
    """Seed DEFAULT/MAS_TRM/MOH templates for an org if not already present."""
    from app.core.models_v12 import VendorEvaluationFramework

    existing = {
        f.framework_type
        for f in db.query(VendorEvaluationFramework.framework_type)
        .filter(VendorEvaluationFramework.organisation_id == org_id).all()
    }
    created = False
    for ftype, spec in _BUILTIN_FRAMEWORKS.items():
        if ftype in existing:
            continue
        w = spec["weights"]
        db.add(VendorEvaluationFramework(
            organisation_id=org_id,
            name=spec["name"],
            framework_type=ftype,
            sector=spec["sector"],
            weight_compliance=w["COMPLIANCE"],
            weight_visibility=w["VISIBILITY"],
            weight_engagement=w["ENGAGEMENT"],
            weight_recency=w["RECENCY"],
            weight_procurement_interest=w["PROCUREMENT_INTEREST"],
            is_builtin=True,
        ))
        created = True
    if created:
        db.commit()


def _framework_dict(fw, active_id=None) -> dict:
    return {
        "id": str(fw.id),
        "name": fw.name,
        "frameworkType": fw.framework_type,
        "sector": fw.sector,
        "weights": fw.weights(),
        "isBuiltin": fw.is_builtin,
        "isActive": str(fw.id) == str(active_id) if active_id else False,
    }


class FrameworkWeights(BaseModel):
    COMPLIANCE: float = Field(ge=0, le=1)
    VISIBILITY: float = Field(ge=0, le=1)
    ENGAGEMENT: float = Field(ge=0, le=1)
    RECENCY: float = Field(ge=0, le=1)
    PROCUREMENT_INTEREST: float = Field(ge=0, le=1)


class FrameworkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    weights: FrameworkWeights
    sector: Optional[str] = Field(default=None, max_length=120)


class FrameworkPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    weights: Optional[FrameworkWeights] = None
    sector: Optional[str] = Field(default=None, max_length=120)


def _validate_weights_sum(w: dict) -> None:
    total = sum(w.get(k, 0) for k in _WEIGHT_KEYS)
    if abs(total - 1.0) > 0.01:
        raise HTTPException(
            status_code=422,
            detail=f"Weights must sum to 1.0 (got {total:.2f}).",
        )


@router.get("/frameworks")
async def list_frameworks(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """List the buyer org's scoring frameworks (built-in templates + custom)."""
    _require_procurement(current_user)
    from app.api.vendor_features import _get_or_create_org

    org = _get_or_create_org(db, current_user)
    _ensure_builtin_frameworks(db, org.id)
    from app.core.models_v12 import VendorEvaluationFramework

    rows = (
        db.query(VendorEvaluationFramework)
        .filter(VendorEvaluationFramework.organisation_id == org.id)
        .order_by(VendorEvaluationFramework.is_builtin.desc(), VendorEvaluationFramework.name)
        .all()
    )
    return {
        "frameworks": [_framework_dict(f, org.active_framework_id) for f in rows],
        "activeFrameworkId": str(org.active_framework_id) if org.active_framework_id else None,
    }


@router.post("/frameworks")
async def create_framework(body: FrameworkCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Create a custom scoring weight profile (Buyer Pro+)."""
    _require_procurement(current_user)
    from app.billing.enforcement import can_customise_frameworks

    plan = (getattr(current_user, "plan", "") or "").lower().strip()
    role = getattr(current_user, "role", "")
    if role != "ADMIN" and not can_customise_frameworks(plan):
        raise HTTPException(
            status_code=403,
            detail="Customisable scoring frameworks require Buyer Professional or higher.",
        )
    w = body.weights.model_dump()
    _validate_weights_sum(w)
    from app.api.vendor_features import _get_or_create_org
    from app.core.models_v12 import VendorEvaluationFramework

    org = _get_or_create_org(db, current_user)
    fw = VendorEvaluationFramework(
        organisation_id=org.id,
        name=body.name.strip(),
        framework_type="CUSTOM",
        sector=(body.sector or "").strip().lower() or None,
        weight_compliance=w["COMPLIANCE"],
        weight_visibility=w["VISIBILITY"],
        weight_engagement=w["ENGAGEMENT"],
        weight_recency=w["RECENCY"],
        weight_procurement_interest=w["PROCUREMENT_INTEREST"],
        is_builtin=False,
    )
    db.add(fw)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="A framework with that name already exists.")
    return _framework_dict(fw, org.active_framework_id)


@router.patch("/frameworks/{framework_id}")
async def update_framework(framework_id: str, body: FrameworkPatch, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Edit a custom framework's weights/sector/name (Buyer Pro+)."""
    _require_procurement(current_user)
    from app.billing.enforcement import can_customise_frameworks

    plan = (getattr(current_user, "plan", "") or "").lower().strip()
    role = getattr(current_user, "role", "")
    if role != "ADMIN" and not can_customise_frameworks(plan):
        raise HTTPException(status_code=403, detail="Editing scoring frameworks requires Buyer Professional or higher.")
    from app.api.vendor_features import _get_or_create_org
    from app.core.models_v12 import VendorEvaluationFramework

    org = _get_or_create_org(db, current_user)
    fw = (
        db.query(VendorEvaluationFramework)
        .filter(VendorEvaluationFramework.id == framework_id, VendorEvaluationFramework.organisation_id == org.id)
        .first()
    )
    if not fw:
        raise HTTPException(status_code=404, detail="Framework not found.")
    if fw.is_builtin:
        raise HTTPException(status_code=403, detail="Built-in templates can't be edited — create a custom framework instead.")
    if body.weights is not None:
        w = body.weights.model_dump()
        _validate_weights_sum(w)
        fw.weight_compliance = w["COMPLIANCE"]
        fw.weight_visibility = w["VISIBILITY"]
        fw.weight_engagement = w["ENGAGEMENT"]
        fw.weight_recency = w["RECENCY"]
        fw.weight_procurement_interest = w["PROCUREMENT_INTEREST"]
    if body.name is not None:
        fw.name = body.name.strip()
    if body.sector is not None:
        fw.sector = (body.sector or "").strip().lower() or None
    db.commit()
    return _framework_dict(fw, org.active_framework_id)


@router.post("/frameworks/{framework_id}/activate")
async def activate_framework(framework_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Set the org's default scoring framework. Sector-scoped templates require
    the Enterprise tier (multiple frameworks); a single custom profile is Pro+."""
    _require_procurement(current_user)
    from app.api.vendor_features import _get_or_create_org
    from app.core.models_v12 import VendorEvaluationFramework

    org = _get_or_create_org(db, current_user)
    fw = (
        db.query(VendorEvaluationFramework)
        .filter(VendorEvaluationFramework.id == framework_id, VendorEvaluationFramework.organisation_id == org.id)
        .first()
    )
    if not fw:
        raise HTTPException(status_code=404, detail="Framework not found.")
    org.active_framework_id = fw.id
    db.commit()
    return {"activeFrameworkId": str(fw.id), "name": fw.name}


@router.get("/scan-verification-log")
async def scan_verification_log(
    vendor_slug: str = Query(..., description="Vendor email-prefix / slug"),
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Buyer Enterprise: on-chain per-scan verification log for one vendor.

    Lists this buyer's scans of the vendor with their Polygon tx_hash (anchored
    asynchronously after each scan) and explorer link.
    """
    _require_procurement(current_user)
    plan = (getattr(current_user, "plan", "") or "").lower().strip()
    role = getattr(current_user, "role", "")
    if role != "ADMIN" and plan not in (
        "buyer_enterprise", "buyer_enterprise_monthly", "buyer_enterprise_annual",
    ):
        raise HTTPException(
            status_code=403,
            detail="On-chain scan verification log is a Buyer Enterprise feature.",
        )

    vendor = db.query(User).filter(User.email.like(f"{vendor_slug}@%")).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    from app.core.models_v8 import VendorScanLedger
    from app.core.config import settings as _settings

    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)
    scans = (
        db.query(VendorScanLedger)
        .filter(
            VendorScanLedger.buyer_id == current_user.id,
            VendorScanLedger.vendor_id == vendor.id,
            VendorScanLedger.created_at >= cutoff,
        )
        .order_by(VendorScanLedger.created_at.desc())
        .all()
    )
    explorer = (getattr(_settings, "POLYGON_EXPLORER_URL", "") or "https://amoy.polygonscan.com").rstrip("/")

    return {
        "vendor": {"company": vendor.company, "slug": vendor_slug},
        "network": getattr(_settings, "POLYGON_NETWORK_NAME", "Polygon Amoy"),
        "periodMonths": months,
        "scans": [
            {
                "id": str(s.id),
                "month": s.month,
                "scanType": s.scan_type,
                "createdAt": s.created_at.isoformat() if s.created_at else None,
                "txHash": s.tx_hash,
                "anchoredAt": s.anchored_at.isoformat() if s.anchored_at else None,
                "status": (
                    "anchored" if s.tx_hash
                    else ("failed" if s.anchor_error else "pending")
                ),
                "explorerUrl": f"{explorer}/tx/{s.tx_hash}" if s.tx_hash else None,
            }
            for s in scans
        ],
        "total": len(scans),
    }
