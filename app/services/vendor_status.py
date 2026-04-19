"""
Vendor Status Engine — V8
=========================
Payment-independent trust assessment system.

All status levels (VerificationDepth, MonitoringActivity, RiskSignal,
procurementReadiness) are derived exclusively from trust facts:
  - VerifyRecord (compliance score, lifecycle, expiry, documents)
  - Proof / ProofView (monitoring activity signals)
  - AnomalyEvent (risk signal)
  - ActivityLog (recency of engagement)

These status levels are NEVER derived from:
  - Stripe subscription or plan
  - Payment history
  - Billing state

IMPORTANT: After computing status, call upsert_status_snapshot() to
write results to VendorStatusSnapshot for procurement query performance.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.core.models import (
    VerifyRecord, ProofView, ActivityLog,
    VendorSector, VendorScore, GovernanceRecord,
)
from app.core.models_v8 import VendorStatusSnapshot, ScoreSnapshot, AnomalyEvent

logger = logging.getLogger(__name__)


def _ensure_aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# ── Logic version — bump when thresholds change (invalidates cached rows) ──────
STATUS_LOGIC_VERSION = "v2"

# ── VerificationDepth thresholds ───────────────────────────────────────────────
DEPTH_COMPLIANCE_THRESHOLDS = {
    "CERTIFIED": 80,
    "DEEP":      60,
    "STANDARD":  30,
    "BASIC":     0,
}
DEPTH_DOC_THRESHOLDS = {
    "CERTIFIED": 5,
    "DEEP":      5,
    "STANDARD":  2,
    "BASIC":     0,
}

# ── Monitoring activity thresholds (days) ──────────────────────────────────────
SNAPSHOT_ACTIVE_DAYS   = 7
SNAPSHOT_STALE_DAYS    = 30
TRUST_EVENT_ACTIVE_DAYS = 14

# ── Risk signal severity ordering ─────────────────────────────────────────────
SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def compute_verification_depth(db: Session, vendor_id: str) -> str:
    """
    Derives verification depth from VerifyRecord facts.
    UNVERIFIED / BASIC / STANDARD / DEEP / CERTIFIED
    """
    record = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == vendor_id
    ).first()

    if not record or record.lifecycle_status.value != "ACTIVE":
        return "UNVERIFIED"

    compliance = record.compliance_score or 0

    # Count documents via Proof rows
    from app.core.models import Proof
    doc_count = db.query(Proof).filter(Proof.verify_id == record.id).count()

    # Check for open HIGH/CRITICAL anomalies (blocks CERTIFIED)
    from app.core.models_v8 import AnomalyEvent
    has_high_anomaly = db.query(AnomalyEvent).filter(
        AnomalyEvent.vendor_id == vendor_id,
        AnomalyEvent.status == "OPEN",
        AnomalyEvent.severity.in_(["HIGH", "CRITICAL"]),
    ).first() is not None

    if (
        compliance >= DEPTH_COMPLIANCE_THRESHOLDS["CERTIFIED"]
        and doc_count >= DEPTH_DOC_THRESHOLDS["CERTIFIED"]
        and not has_high_anomaly
    ):
        return "CERTIFIED"
    elif compliance >= DEPTH_COMPLIANCE_THRESHOLDS["DEEP"] and doc_count >= DEPTH_DOC_THRESHOLDS["DEEP"]:
        return "DEEP"
    elif compliance >= DEPTH_COMPLIANCE_THRESHOLDS["STANDARD"] and doc_count >= DEPTH_DOC_THRESHOLDS["STANDARD"]:
        return "STANDARD"
    else:
        return "BASIC"


def compute_monitoring_activity(db: Session, vendor_id: str) -> str:
    """
    Derives monitoring activity from ScoreSnapshot recency and ActivityLog.
    ACTIVE / STALE / INACTIVE / NONE
    """
    latest_snapshot = db.query(ScoreSnapshot).filter(
        ScoreSnapshot.vendor_id == vendor_id
    ).order_by(ScoreSnapshot.snapshot_at.desc()).first()

    if not latest_snapshot:
        return "NONE"

    now = datetime.now(timezone.utc)
    snapshot_age_days = (now - _ensure_aware(latest_snapshot.snapshot_at)).days

    latest_activity = db.query(ActivityLog).filter(
        ActivityLog.user_id == vendor_id
    ).order_by(ActivityLog.created_at.desc()).first()

    has_recent_event = (
        latest_activity is not None
        and (now - _ensure_aware(latest_activity.created_at)).days <= TRUST_EVENT_ACTIVE_DAYS
    )

    if snapshot_age_days > SNAPSHOT_STALE_DAYS:
        return "INACTIVE"
    elif snapshot_age_days <= SNAPSHOT_ACTIVE_DAYS and has_recent_event:
        return "ACTIVE"
    else:
        return "STALE"


def compute_risk_signal(db: Session, vendor_id: str) -> str:
    """
    Derives risk signal from AnomalyEvent.status='OPEN'.
    CLEAN / WATCH / FLAGGED / CRITICAL
    """
    open_anomalies = db.query(AnomalyEvent).filter(
        AnomalyEvent.vendor_id == vendor_id,
        AnomalyEvent.status == "OPEN",
    ).all()

    if not open_anomalies:
        return "CLEAN"

    highest = "LOW"
    for a in open_anomalies:
        sev = (a.severity or "LOW").upper()
        if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(highest, 0):
            highest = sev

    if highest == "CRITICAL":
        return "CRITICAL"
    elif highest == "HIGH":
        return "FLAGGED"
    else:
        return "WATCH"


def compute_procurement_readiness(
    verification_depth: str,
    monitoring_activity: str,
    risk_signal: str,
) -> str:
    """
    Composite procurement readiness.
    READY / CONDITIONAL / NEEDS_ATTENTION / NOT_READY
    """
    depth_rank = {"UNVERIFIED": 0, "BASIC": 1, "STANDARD": 2, "DEEP": 3, "CERTIFIED": 4}
    monitoring_rank = {"NONE": 0, "INACTIVE": 1, "STALE": 2, "ACTIVE": 3}
    risk_rank = {"CRITICAL": 3, "FLAGGED": 2, "WATCH": 1, "CLEAN": 0}

    d = depth_rank.get(verification_depth, 0)
    m = monitoring_rank.get(monitoring_activity, 0)
    r = risk_rank.get(risk_signal, 0)

    if r >= 2:               # FLAGGED or CRITICAL
        return "NOT_READY"
    elif d >= 3 and m >= 2:  # DEEP/CERTIFIED + STALE/ACTIVE + clean
        return "READY"
    elif d >= 2:             # STANDARD+
        return "CONDITIONAL"
    elif d >= 1:             # BASIC
        return "NEEDS_ATTENTION"
    else:
        return "NOT_READY"


def get_readiness_summary(readiness: str, depth: str, monitoring: str, risk: str) -> str:
    """Human-readable one-liner for the status profile."""
    if readiness == "READY":
        return f"Verified ({depth}) with {monitoring.lower()} monitoring and clean risk profile."
    elif readiness == "CONDITIONAL":
        return f"Meets baseline ({depth}), monitoring is {monitoring.lower()}."
    elif readiness == "NEEDS_ATTENTION":
        return f"Basic verification only ({depth}). Consider adding more compliance documents."
    elif risk in ("FLAGGED", "CRITICAL"):
        return f"Open risk signals ({risk}) require resolution before procurement qualification."
    else:
        return "No active verification found. Begin the verification process to appear in procurement results."


def get_vendor_status(db: Session, vendor_id: str) -> dict:
    """
    Full VendorStatusProfile for a single vendor.
    Returns a dict safe to serialise as JSON.
    """
    depth       = compute_verification_depth(db, vendor_id)
    monitoring  = compute_monitoring_activity(db, vendor_id)
    risk        = compute_risk_signal(db, vendor_id)
    readiness   = compute_procurement_readiness(depth, monitoring, risk)
    summary     = get_readiness_summary(readiness, depth, monitoring, risk)

    # Verification detail
    record = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
    expiry   = record.expires_at if record else None
    days_until_expiry = None
    is_expiring_soon  = False
    if expiry:
        days_until_expiry = (_ensure_aware(expiry) - datetime.now(timezone.utc)).days
        is_expiring_soon  = days_until_expiry <= 30

    from app.core.models import Proof
    doc_count = 0
    if record:
        doc_count = db.query(Proof).filter(Proof.verify_id == record.id).count()

    # Use dynamic VendorScore.compliance_score if available; fall back to VerifyRecord baseline
    score_row = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
    live_compliance_score = (
        score_row.compliance_score
        if score_row and score_row.compliance_score
        else (record.compliance_score if record else 0)
    )

    # Monitoring detail
    latest_snapshot = db.query(ScoreSnapshot).filter(
        ScoreSnapshot.vendor_id == vendor_id
    ).order_by(ScoreSnapshot.snapshot_at.desc()).first()

    latest_activity = db.query(ActivityLog).filter(
        ActivityLog.user_id == vendor_id
    ).order_by(ActivityLog.created_at.desc()).first()

    activity_7d = db.query(ActivityLog).filter(
        ActivityLog.user_id == vendor_id,
        ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
    ).count()

    total_activities = db.query(ActivityLog).filter(
        ActivityLog.user_id == vendor_id
    ).count()

    proof_views_30d = 0
    if record:
        proof_views_30d = db.query(ProofView).filter(
            ProofView.verify_id == record.id,
            ProofView.created_at >= datetime.now(timezone.utc) - timedelta(days=30),
        ).count()

    snapshot_age_days = None
    if latest_snapshot:
        snapshot_age_days = (datetime.now(timezone.utc) - _ensure_aware(latest_snapshot.snapshot_at)).days

    # Risk detail — query AnomalyEvent directly
    open_anomalies = db.query(AnomalyEvent).filter(
        AnomalyEvent.vendor_id == vendor_id,
        AnomalyEvent.status == "OPEN",
    ).all()

    highest_severity = None
    anomaly_list = []
    for a in open_anomalies:
        sev = (a.severity or "LOW").upper()
        if highest_severity is None or SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(highest_severity, 0):
            highest_severity = sev
        anomaly_list.append({
            "type":     a.anomaly_type,
            "severity": sev,
            "since":    a.detected_at.isoformat() if a.detected_at else None,
        })

    # Sector percentile from latest snapshot
    sector_pct = latest_snapshot.sector_percentile if latest_snapshot else 50.0

    return {
        "vendorId":            str(vendor_id),
        "verificationDepth":   depth,
        "verificationDetail": {
            "lifecycleStatus":    record.lifecycle_status.value if record else "NONE",
            "complianceScore":    live_compliance_score,
            "documentsSubmitted": doc_count,
            "expiresAt":          expiry.isoformat() if expiry else None,
            "daysUntilExpiry":    days_until_expiry,
            "isExpiringSoon":     is_expiring_soon,
        },
        "monitoringActivity":  monitoring,
        "monitoringDetail": {
            "lastSnapshotAt":    latest_snapshot.snapshot_at.isoformat() if latest_snapshot else None,
            "snapshotAgeDays":   snapshot_age_days,
            "lastTrustEventAt":  latest_activity.created_at.isoformat() if latest_activity else None,
            "trustEventCount7d": activity_7d,
            "totalTrustEvents":  total_activities,
            "proofViewCount30d": proof_views_30d,
        },
        "riskSignal":          risk,
        "riskDetail": {
            "openAnomalyCount": len(open_anomalies),
            "highestSeverity":  highest_severity,
            "openAnomalies":    anomaly_list,
        },
        "procurementReadiness": readiness,
        "readinessSummary":     summary,
        "sectorPercentile":     sector_pct,
        "computedAt":           datetime.now(timezone.utc).isoformat(),
        "cacheKeyVersion":      STATUS_LOGIC_VERSION,
    }


def get_batch_vendor_status(db: Session, vendor_ids: list) -> dict:
    """Batch variant — returns {vendor_id: status_dict}."""
    return {vid: get_vendor_status(db, vid) for vid in vendor_ids}


def upsert_status_snapshot(db: Session, vendor_id: str) -> VendorStatusSnapshot:
    """
    Write-through cache: compute status and persist to VendorStatusSnapshot.
    Call this after every ScoreSnapshot write.
    """
    status = get_vendor_status(db, vendor_id)

    # Pull elevation data if available
    from app.services.notarization_elevation import fetch_elevation_metadata
    elevation = fetch_elevation_metadata(db, vendor_id)

    notarization_depth = 0
    if elevation.get("structural_level") == "ELEVATED":
        depth_map = {"BASIC": 1, "ENHANCED": 2, "DEEP": 3, "ENTERPRISE": 4}
        notarization_depth = depth_map.get(elevation.get("verification_depth", ""), 0)

    dual_silent_mode = (
        "ELEVATED_VERIFIED" if elevation.get("structural_level") == "ELEVATED"
        else "SILENT_RISK_CAPTURE"
    )

    existing = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id == vendor_id
    ).first()

    if existing:
        existing.verification_depth     = status["verificationDepth"]
        existing.monitoring_activity    = status["monitoringActivity"]
        existing.risk_signal            = status["riskSignal"]
        existing.procurement_readiness  = status["procurementReadiness"]
        existing.risk_adjusted_pct      = status["sectorPercentile"]
        existing.dual_silent_mode       = dual_silent_mode
        existing.notarization_depth     = notarization_depth
        existing.evidence_count         = elevation.get("evidence_count", 0)
        existing.confidence_score       = elevation.get("confidence_score", 0.0)
        existing.version                = STATUS_LOGIC_VERSION
        existing.computed_at            = datetime.now(timezone.utc)
        snapshot = existing
    else:
        snapshot = VendorStatusSnapshot(
            vendor_id              = vendor_id,
            verification_depth     = status["verificationDepth"],
            monitoring_activity    = status["monitoringActivity"],
            risk_signal            = status["riskSignal"],
            procurement_readiness  = status["procurementReadiness"],
            risk_adjusted_pct      = status["sectorPercentile"],
            dual_silent_mode       = dual_silent_mode,
            notarization_depth     = notarization_depth,
            evidence_count         = elevation.get("evidence_count", 0),
            confidence_score       = elevation.get("confidence_score", 0.0),
            version                = STATUS_LOGIC_VERSION,
            computed_at            = datetime.now(timezone.utc),
        )
        db.add(snapshot)

    db.commit()
    db.refresh(snapshot)
    return snapshot


def record_score_snapshot(
    db: Session,
    vendor_id: str,
    vendor_score,          # VendorScore ORM object
    sector: Optional[str] = None,
) -> ScoreSnapshot:
    """
    Creates a new ScoreSnapshot row from the current VendorScore state.
    Called after VendorScoreEngine.update_vendor_score().
    Also triggers upsert_status_snapshot for procurement cache freshness.
    """
    from datetime import datetime, timezone
    import hashlib, json

    now      = datetime.now(timezone.utc)
    quarter  = f"Q{((now.month - 1) // 3) + 1} {now.year}"
    breakdown = {
        "compliance":             vendor_score.compliance_score,
        "visibility":             vendor_score.visibility_score,
        "engagement":             vendor_score.engagement_score,
        "recency":                vendor_score.recency_score,
        "procurementInterest":    vendor_score.procurement_interest_score,
        "total":                  vendor_score.total_score,
    }

    # Sector percentile: simple rank within sector (approximation)
    pct = 50.0
    if sector:
        from app.core.models import VendorSector
        sector_vendors = db.query(VendorSector.vendor_id).filter(
            VendorSector.sector == sector
        ).all()
        sector_ids = [str(v[0]) for v in sector_vendors]
        if sector_ids:
            lower = db.query(VendorScore).filter(
                VendorScore.vendor_id.in_(sector_ids),
                VendorScore.total_score < vendor_score.total_score,
            ).count()
            total = max(len(sector_ids), 1)
            pct = round((lower / total) * 100, 1)

    score_version = "1.0"
    raw = f"{vendor_id}:{score_version}:{json.dumps(breakdown, sort_keys=True)}:{pct}"
    score_hash = hashlib.sha256(raw.encode()).hexdigest()

    ss = ScoreSnapshot(
        vendor_id        = vendor_id,
        base_score       = float(vendor_score.total_score),
        multiplier       = 1.0,
        final_score      = vendor_score.total_score,
        breakdown        = breakdown,
        sector_percentile= pct,
        score_version    = score_version,
        score_hash       = score_hash,
        quarter          = quarter,
        snapshot_at      = now,
    )
    db.add(ss)
    db.commit()
    db.refresh(ss)

    # Keep procurement cache fresh
    try:
        upsert_status_snapshot(db, vendor_id)
    except Exception as e:
        logger.warning(f"upsert_status_snapshot failed for {vendor_id}: {e}")

    return ss
