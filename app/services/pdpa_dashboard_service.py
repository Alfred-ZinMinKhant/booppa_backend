"""
PDPA Monitor / Compliance Dashboard — data aggregation
======================================================
Powers the in-app PDPA dashboard (`GET /api/v1/pdpa/dashboard`). Turns the
subscription from an email-only product into a living workspace by surfacing,
in one call, what previously lived only in monthly PDFs:

  • compliance-score trend over scans (clickable into each report)
  • open findings with *days-open* aging + inline remediation status
  • compliance drift events (previously only on the buyer side)
  • a persistent scan-history list (fixes the session-URL-only dead end)

Design notes
------------
The open-findings aging here deliberately uses the canonical stable finding
keys (`extract_finding_keys` / `label_for_key`) — the SAME scheme the
remediation API validates against — so the dashboard's "I fixed this" action
maps 1:1 to what the user sees. This is intentionally independent of the
monthly-email aging in `tasks.py` (which keys on raw finding `type`), so this
module can evolve without risking the email's output.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.models import Report
from app.core.models_v8 import ComplianceDriftEvent, FindingRemediation
from app.services.finding_keys import extract_finding_keys, label_for_key

# Frameworks that count as a "PDPA scan" for trend/history purposes.
_PDPA_FRAMEWORKS = ("pdpa_quick_scan", "pdpa_snapshot")

# Severity inferred from the stable finding-key prefix. Deterministic and
# explainable — no LLM, no per-scan variance.
_KEY_SEVERITY = {
    "nric": "HIGH",
    "breach": "HIGH",
    "dim": "MEDIUM",
    "clause": "MEDIUM",
    "xbt": "MEDIUM",
    "tracker": "LOW",
}

_URGENT_DAYS = 14  # HIGH findings open longer than this are flagged urgent.


def _severity_for_key(key: str) -> str:
    return _KEY_SEVERITY.get(key.split(":", 1)[0], "MEDIUM")


def _compliance_score(report: Report) -> Optional[int]:
    """Canonical persisted score; fall back to 100 - risk. None if unknown."""
    ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
    cs = ad.get("compliance_score")
    if isinstance(cs, (int, float)):
        return max(0, min(100, int(round(cs))))
    for k in ("overall_risk_score", "risk_score", "score"):
        v = ad.get(k)
        if isinstance(v, (int, float)):
            return max(0, min(100, 100 - int(round(v))))
    return None


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _completed_pdpa_reports(db: Session, vendor_id, *, ascending: bool, limit: int) -> List[Report]:
    order = Report.completed_at.asc().nullsfirst() if ascending else Report.completed_at.desc().nullslast()
    return (
        db.query(Report)
        .filter(
            Report.owner_id == vendor_id,
            Report.framework.in_(_PDPA_FRAMEWORKS),
            Report.status == "completed",
        )
        .order_by(order)
        .limit(limit)
        .all()
    )


def build_pdpa_dashboard(db: Session, vendor_id) -> Dict[str, Any]:
    """Assemble the full PDPA dashboard payload for one vendor."""
    # Newest-first for headline + history; reuse for the ascending views.
    history_desc = _completed_pdpa_reports(db, vendor_id, ascending=False, limit=12)

    if not history_desc:
        return {
            "latestScore": None,
            "scoreDelta": None,
            "lastScannedAt": None,
            "scannedUrl": None,
            "trend": [],
            "openFindings": [],
            "driftEvents": _drift_events(db, vendor_id),
            "scanHistory": [],
        }

    current = history_desc[0]
    previous = history_desc[1] if len(history_desc) > 1 else None
    cur_score = _compliance_score(current)
    prev_score = _compliance_score(previous) if previous else None
    score_delta = (cur_score - prev_score) if (cur_score is not None and prev_score is not None) else None

    cur_ad = current.assessment_data if isinstance(current.assessment_data, dict) else {}
    scanned_url = cur_ad.get("display_url") or cur_ad.get("website_url") or current.company_website

    return {
        "latestScore": cur_score,
        "scoreDelta": score_delta,
        "lastScannedAt": (current.completed_at or current.created_at).isoformat() if (current.completed_at or current.created_at) else None,
        "scannedUrl": scanned_url,
        "trend": _trend_points(db, vendor_id),
        "openFindings": _open_findings(db, vendor_id, current, history_desc),
        "driftEvents": _drift_events(db, vendor_id),
        "scanHistory": _scan_history(history_desc),
    }


def _trend_points(db: Session, vendor_id) -> List[Dict[str, Any]]:
    """Up to 8 most recent scans, oldest → newest, each clickable into its report."""
    rows = _completed_pdpa_reports(db, vendor_id, ascending=False, limit=8)
    seen_months = set()
    monthly_latest = []
    for r in rows:
        when = r.completed_at or r.created_at
        if not when:
            continue
        m_key = when.strftime("%Y-%m")
        if m_key not in seen_months:
            seen_months.add(m_key)
            monthly_latest.append(r)
            if len(monthly_latest) == 8:
                break

    points: List[Dict[str, Any]] = []
    for r in reversed(monthly_latest):  # oldest → newest
        score = _compliance_score(r)
        when = r.completed_at or r.created_at
        if score is not None and when is not None:
            points.append({
                "label": when.strftime("%b %y"),
                "score": score,
                "reportId": str(r.id),
                "completedAt": when.isoformat(),
            })
    return points


def _open_findings(db: Session, vendor_id, current: Report, history_desc: List[Report]) -> List[Dict[str, Any]]:
    """Findings present in the latest scan, with days-open age + remediation status."""
    current_keys = extract_finding_keys(current.assessment_data)
    if not current_keys:
        return []

    # Earliest-seen timestamp per stable key, walking history oldest → newest.
    first_seen: Dict[str, datetime] = {}
    for r in reversed(history_desc):  # ascending
        when = _naive(r.completed_at or r.created_at)
        if when is None:
            continue
        for k in extract_finding_keys(r.assessment_data):
            first_seen.setdefault(k, when)

    # Latest remediation row per finding key for this vendor (status badge).
    rem_rows = (
        db.query(FindingRemediation)
        .filter(FindingRemediation.vendor_id == vendor_id)
        .order_by(FindingRemediation.marked_at.desc())
        .all()
    )
    rem_by_key: Dict[str, FindingRemediation] = {}
    for row in rem_rows:
        rem_by_key.setdefault(row.finding_key, row)

    now = datetime.utcnow()
    out: List[Dict[str, Any]] = []
    for key in current_keys:
        seen = first_seen.get(key)
        days_open = (now - seen).days if seen else 0
        severity = _severity_for_key(key)
        rem = rem_by_key.get(key)
        out.append({
            "findingKey": key,
            "label": label_for_key(key),
            "severity": severity,
            "daysOpen": days_open,
            "firstSeen": seen.isoformat() if seen else None,
            "urgent": severity == "HIGH" and days_open > _URGENT_DAYS,
            "reportId": str(current.id),
            "remediationStatus": (rem.status if rem else None),
            "remediationConfirmation": (rem.confirmation_status if rem else None),
        })

    # Most urgent first: urgent flag, then severity weight, then age.
    sev_weight = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    out.sort(key=lambda f: (f["urgent"], sev_weight.get(f["severity"], 0), f["daysOpen"]), reverse=True)
    return out


def _drift_events(db: Session, vendor_id) -> List[Dict[str, Any]]:
    """Recent compliance-drift events for this vendor (previously buyer-side only)."""
    rows = (
        db.query(ComplianceDriftEvent)
        .filter(ComplianceDriftEvent.vendor_id == vendor_id)
        .order_by(ComplianceDriftEvent.created_at.desc())
        .limit(10)
        .all()
    )
    return [
        {
            "framework": d.framework,
            "severity": d.severity,
            "previousScore": d.previous_score,
            "currentScore": d.current_score,
            "delta": d.delta,
            "deltaPct": d.delta_pct,
            "occurredAt": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


def _scan_history(history_desc: List[Report]) -> List[Dict[str, Any]]:
    """Persistent list of past scans (newest first) with score + delta + PDF link."""
    scored = [(r, _compliance_score(r)) for r in history_desc]
    out: List[Dict[str, Any]] = []
    for i, (r, score) in enumerate(scored):
        # Delta vs the next-newer-down (i.e. the previous scan chronologically).
        prev_score = scored[i + 1][1] if i + 1 < len(scored) else None
        delta = (score - prev_score) if (score is not None and prev_score is not None) else None
        when = r.completed_at or r.created_at
        out.append({
            "reportId": str(r.id),
            "date": when.isoformat() if when else None,
            "score": score,
            "delta": delta,
            "pdfUrl": r.s3_url or None,
        })
    return out
