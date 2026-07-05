"""Single source of truth for reading PDPA findings out of a Report's
``assessment_data``.

Historically, findings were persisted under several different keys depending on
the version of the PDPA worker that produced the report. The modern Quick Scan
(``process_report_task``) nests the structured report under
``assessment_data["booppa_report"]`` and puts findings at
``booppa_report["detailed_findings"]`` — but older paths (and some AI code
paths) wrote them at the top level or under ``risk_assessment``.

The Cover Sheet already dereferenced ``booppa_report`` first and got the right
count; the Monitor Report read only the top-level keys and therefore always saw
zero — the "0 vs 2 open findings" contradiction. This helper centralises the
lookup so every consumer resolves the same list from the same row.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_pdpa_findings(assessment_data: Any) -> list:
    """Return the list of PDPA findings from a Report's ``assessment_data``.

    Checks the modern nested location (``booppa_report.detailed_findings``)
    first, then falls back through every legacy key that has held findings.
    Coerces a dict-of-findings into a list and always returns a list.
    """
    ad = assessment_data if isinstance(assessment_data, dict) else {}

    structured = ad.get("booppa_report")
    structured = structured if isinstance(structured, dict) else {}
    structured_ra = structured.get("risk_assessment")
    structured_ra = structured_ra if isinstance(structured_ra, dict) else {}
    top_ra = ad.get("risk_assessment")
    top_ra = top_ra if isinstance(top_ra, dict) else {}

    findings = (
        structured.get("detailed_findings")
        or ad.get("detailed_findings")
        or structured.get("findings")
        or ad.get("findings")
        or structured_ra.get("findings")
        or top_ra.get("findings")
        or ad.get("violations")
        or []
    )

    # Some older AI paths return {"finding_key": {...}, ...} instead of a list.
    if isinstance(findings, dict):
        findings = list(findings.values())
    if not isinstance(findings, list):
        findings = []

    return findings


def resolve_pdpa_score(assessment_data: Any) -> Optional[int]:
    """Return the 0–100 compliance score from a Report's ``assessment_data``.

    Single source of truth so the Cover Sheet and the RFP Supplier Declaration
    never disagree (the "66 vs not available" contradiction). PDPA reports
    persist a *compliance* score under ``compliance_score`` — display THAT
    verbatim when present. Only when it's absent do we derive it from the raw
    *risk* score (0 = clean, 100 = high risk) as ``100 - risk``.
    """
    ad = assessment_data if isinstance(assessment_data, dict) else {}

    canonical = ad.get("compliance_score")
    if isinstance(canonical, (int, float)):
        return int(round(canonical))

    structured = ad.get("booppa_report")
    structured = structured if isinstance(structured, dict) else {}
    structured_ra = structured.get("risk_assessment")
    structured_ra = structured_ra if isinstance(structured_ra, dict) else {}

    raw_risk = (
        ad.get("overall_risk_score")
        if ad.get("overall_risk_score") is not None
        else ad.get("score")
        if ad.get("score") is not None
        else ad.get("risk_score")
        if ad.get("risk_score") is not None
        else structured_ra.get("score")
    )
    if raw_risk is None:
        return None
    try:
        return max(0, min(100, 100 - int(round(float(raw_risk)))))
    except (TypeError, ValueError):
        return None


def latest_pdpa_score(db: Any, user_id: Any) -> Optional[int]:
    """Resolve the compliance score from a user's most recent PDPA report.

    Mirrors exactly what the Compliance Evidence Cover Sheet reads, so the RFP
    Supplier Declaration prints the same number instead of "not available".
    Best-effort: returns ``None`` on any lookup/parse failure.
    """
    if db is None or user_id is None:
        return None
    try:
        from app.core.models import Report

        report = (
            db.query(Report)
            .filter(
                Report.owner_id == user_id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        if not report:
            return None
        return resolve_pdpa_score(report.assessment_data)
    except Exception as exc:  # noqa: BLE001 — never block declaration on this
        logger.warning("latest_pdpa_score lookup failed for %s: %s", user_id, exc)
        return None
