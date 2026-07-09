"""
Compliance drift detection for PDPA Monitor subscribers.

Compares the most recent completed PDPA report for a vendor against the
previous one. If the risk_score worsens by more than DRIFT_THRESHOLD_PCT
(default 10%), a ComplianceDriftEvent row is written and an alert email
is sent.

PDPA scoring convention in this codebase:
  - risk_score is a 0-100 number where HIGHER = WORSE risk.
  - "Drift" here means risk increased materially since the last scan.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DRIFT_THRESHOLD_PCT = 10.0  # at least a 10% relative jump in risk to flag


def _extract_risk_score(report) -> Optional[float]:
    data = report.assessment_data if isinstance(report.assessment_data, dict) else {}
    score = data.get("risk_score")
    if score is None and isinstance(data.get("risk_assessment"), dict):
        score = data["risk_assessment"].get("score")
    try:
        return float(score) if score is not None else None
    except (TypeError, ValueError):
        return None


def _classify_severity(delta_pct: float) -> str:
    if delta_pct >= 25:
        return "CRITICAL"
    if delta_pct >= 10:
        return "WARNING"
    return "INFO"


def _per_dimension_flips(
    db: Session,
    vendor_id: str,
    framework: str,
    current_report_id,
    previous_report_id,
) -> list[dict]:
    """Read snapshots from pdpa_dimension_history and return per-dimension
    transitions that WORSENED (Compliant→Partial/Non-Compliant, Partial→Non-Compliant).

    Empty list if either side has no snapshots (e.g., scan happened before
    Tier 4 was deployed). Never raises — falls back to empty on any error.
    """
    try:
        from app.core.models import PdpaDimensionHistory
        from app.services.pdpa_dimension_snapshot import diff_snapshots

        def _load(report_id):
            rows = (
                db.query(PdpaDimensionHistory)
                .filter(
                    PdpaDimensionHistory.vendor_id == vendor_id,
                    PdpaDimensionHistory.framework == framework,
                    PdpaDimensionHistory.report_id == report_id,
                )
                .all()
            )
            return [
                {"dimension_name": r.dimension_name, "status": r.status, "score": r.score}
                for r in rows
            ]

        previous = _load(previous_report_id)
        current = _load(current_report_id)
        if not previous or not current:
            return []
        return diff_snapshots(previous, current)
    except Exception as e:
        logger.warning("Per-dimension flip detection failed: %s", e)
        return []


def detect_drift_for_vendor(
    db: Session,
    vendor_id: str,
    framework: str = "pdpa_quick_scan",
) -> Optional[dict]:
    """
    Compare the latest two completed reports for this vendor + framework.
    If risk has increased by more than DRIFT_THRESHOLD_PCT, persist a
    ComplianceDriftEvent and return its summary dict. Otherwise return None.

    Tier 4: also reads pdpa_dimension_history and attaches per-dimension
    worsened flips to the event's `details.dimension_flips` so monthly emails
    can show "Cookie Consent: Compliant → Non-Compliant" alongside the score
    delta. A drift event is now ALSO emitted when one or more dimensions
    flipped from Compliant to Non-Compliant, even if the overall risk score
    did not move by DRIFT_THRESHOLD_PCT (a dimension flip is a real regression
    that overall averaging can hide).
    """
    from app.core.models import Report
    from app.core.models import ComplianceDriftEvent

    reports = (
        db.query(Report)
        .filter(
            Report.owner_id == vendor_id,
            Report.framework == framework,
            Report.status == "completed",
        )
        .order_by(Report.created_at.desc())
        .limit(2)
        .all()
    )
    if len(reports) < 2:
        return None

    current, previous = reports[0], reports[1]
    current_score = _extract_risk_score(current)
    previous_score = _extract_risk_score(previous)
    if current_score is None or previous_score is None:
        return None

    delta = current_score - previous_score
    if previous_score <= 0:
        delta_pct = 100.0 if delta > 0 else 0.0
    else:
        delta_pct = (delta / previous_score) * 100.0

    dim_flips = _per_dimension_flips(
        db, vendor_id, framework, current.id, previous.id,
    )
    # Critical flip = any dimension going to Non-Compliant
    critical_flips = [f for f in dim_flips if f["current_status"] == "Non-Compliant"]

    if delta_pct < DRIFT_THRESHOLD_PCT and not critical_flips:
        return None

    if critical_flips:
        severity = "CRITICAL"
    else:
        severity = _classify_severity(delta_pct)

    event = ComplianceDriftEvent(
        vendor_id=vendor_id,
        framework=framework,
        previous_report_id=previous.id,
        current_report_id=current.id,
        previous_score=previous_score,
        current_score=current_score,
        delta=delta,
        delta_pct=delta_pct,
        severity=severity,
        details={
            "previous_created_at": previous.created_at.isoformat() if previous.created_at else None,
            "current_created_at": current.created_at.isoformat() if current.created_at else None,
            "previous_risk_level": (previous.assessment_data or {}).get("risk_level"),
            "current_risk_level": (current.assessment_data or {}).get("risk_level"),
            "dimension_flips": dim_flips,
        },
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    logger.info(
        "[ComplianceDrift] vendor=%s framework=%s severity=%s prev=%.1f cur=%.1f delta_pct=%.1f",
        vendor_id, framework, severity, previous_score, current_score, delta_pct,
    )

    return {
        "event_id": str(event.id),
        "severity": severity,
        "previous_score": previous_score,
        "current_score": current_score,
        "delta": delta,
        "delta_pct": delta_pct,
        "dimension_flips": dim_flips,
    }
