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


def resolve_pdpa_risk_score(assessment_data: Any) -> Optional[int]:
    """Return the raw 0–100 *risk* score (0 = clean, 100 = high risk), or None.

    Single source of truth for "has the scan produced a score yet?". The score
    lives in one of four places depending on which path wrote it, and reading
    only a subset is how paid Quick Scans got stranded: the fulfillment gate
    checked top-level ``risk_score`` / ``risk_assessment.score``, neither of
    which ``process_report_task`` ever writes — it persists the AI score nested
    under ``booppa_report.risk_assessment.score``.
    """
    ad = assessment_data if isinstance(assessment_data, dict) else {}

    structured = ad.get("booppa_report")
    structured = structured if isinstance(structured, dict) else {}
    structured_ra = structured.get("risk_assessment")
    structured_ra = structured_ra if isinstance(structured_ra, dict) else {}
    top_ra = ad.get("risk_assessment")
    top_ra = top_ra if isinstance(top_ra, dict) else {}

    raw_risk = (
        ad.get("overall_risk_score")
        if ad.get("overall_risk_score") is not None
        else ad.get("score")
        if ad.get("score") is not None
        else ad.get("risk_score")
        if ad.get("risk_score") is not None
        else structured_ra.get("score")
        if structured_ra.get("score") is not None
        else top_ra.get("score")
    )
    if raw_risk is None:
        return None
    try:
        return max(0, min(100, int(round(float(raw_risk)))))
    except (TypeError, ValueError):
        return None


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

    raw_risk = resolve_pdpa_risk_score(ad)
    if raw_risk is None:
        return None
    return max(0, min(100, 100 - raw_risk))


def _normalise_host(url: Optional[str]) -> Optional[str]:
    """Bare lowercase hostname from a URL or hostname, ``None`` if unusable."""
    if not url:
        return None
    raw = str(url).strip().lower()
    if not raw:
        return None
    if "//" not in raw:
        raw = "//" + raw
    try:
        from urllib.parse import urlsplit

        host = urlsplit(raw).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    host = host.removeprefix("www.")
    return host or None


def latest_pdpa_score(db: Any, user_id: Any, domain: Optional[str] = None) -> Optional[int]:
    """Resolve the compliance score from a user's most recent PDPA report.

    Mirrors exactly what the Compliance Evidence Cover Sheet reads, so the RFP
    Supplier Declaration prints the same number instead of "not available".
    Best-effort: returns ``None`` on any lookup/parse failure.

    ``domain`` scopes the lookup to reports scanned for that website. Accounts
    get reused across companies, so an account-wide "latest score" can print one
    company's score in another company's certified deliverable — the same
    account-cache contamination as the SPQR UEN leak. Callers rendering a
    report-scoped document should always pass it; omitting it keeps the legacy
    account-wide behaviour for genuinely account-scoped products.
    """
    if db is None or user_id is None:
        return None
    try:
        from app.core.models import Report

        # Prefer COMPLETED reports: a scan queued concurrently with the RFP kit
        # may still be 'pending' (no score persisted yet). Grabbing the newest
        # row regardless of status returned None even when an older completed
        # scan (e.g. 61/100) existed — that was the D4 "not available" bug.
        # Scan the recent history and return the first report that yields a
        # score, so a fresh-but-unfinished scan can't shadow a finished one.
        reports = (
            db.query(Report)
            .filter(
                Report.owner_id == user_id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .limit(5)
            .all()
        )
        want_host = _normalise_host(domain)
        for report in reports:
            if want_host and _normalise_host(report.company_website) != want_host:
                continue
            score = resolve_pdpa_score(report.assessment_data)
            if score is not None:
                return score
        return None
    except Exception as exc:  # noqa: BLE001 — never block declaration on this
        logger.warning("latest_pdpa_score lookup failed for %s: %s", user_id, exc)
        return None
