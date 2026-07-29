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


# Dimensions whose verdict contradicts a "no violations" narrative.
FAILING_STATUSES = ("Non-Compliant", "Partial")

# Per-dimension finding synthesis. Owned here rather than in `pdf_service`
# because the Quick Scan PDF is no longer the only consumer: the Compliance
# Cover Sheet renders its own findings list and risk level, and a copy of this
# table living next to one renderer is how two documents in the SAME bundle
# came to describe the same scan differently.
#
# For each scored dimension:
#   [0] keywords meaning "an existing finding already covers this dimension".
#   [1] the finding `type` slug — chosen so `pdpc_precedents.finding_category`
#       routes it to the correct statutory basis via _CATEGORY_SUBSTRINGS.
#   [2] remediation requirements rendered as the §5 task body.
DIMENSION_FINDING_SPEC: dict[str, tuple[list[str], str, list[str]]] = {
    "Cookie Consent Mechanism": (
        # "cookie_violation" is the legacy `_detect_violations` slug for this same
        # gap. Without it the dedup misses and the buyer sees the dimension twice.
        # Note it is NOT a substring of "cookie_attributes_violation", so a Cookie
        # Attributes finding still cannot suppress a Cookie Consent one.
        ["consent", "cookie_consent", "no_consent_banner", "tracking_cookie", "cookie_violation"],
        "cookie_consent_violation",
        [
            "Block all non-essential trackers until the user gives affirmative consent.",
            "Present a consent banner with granular opt-in (no pre-ticked boxes).",
            "Provide a withdrawal mechanism that is as easy to use as giving consent.",
        ],
    ),
    "Third-Party Tracker Inventory": (
        ["tracker", "third_party", "third-party"],
        "tracker_inventory_violation",
        [
            "Inventory every third-party script and the personal data it collects.",
            "Gate each non-essential tracker behind the consent mechanism.",
            "Record the lawful basis for each tracker retained.",
        ],
    ),
    "Privacy Policy (PDPA §11/13)": (
        # "organizational_violation" is the legacy slug for the missing-DPO check,
        # which this dimension also covers (§11(3)).
        ["privacy_policy", "no_privacy_policy", "privacy policy", "dpo", "no_dpo_contact",
         "organizational_violation"],
        "privacy_policy_violation",
        [
            "Publish the §13 clauses identified as missing in Section 4.",
            "Disclose the DPO's business contact information (PDPA §11(3)).",
        ],
    ),
    "Security HTTP Headers": (
        ["hsts", "csp", "x_frame", "x_content_type", "referrer", "security_headers", "https"],
        "security_headers_violation",
        ["Set the missing response headers named in Section 4 at the edge or origin."],
    ),
    "Cookie Attributes": (
        ["cookie_secure", "secure flag", "httponly", "samesite"],
        "cookie_attributes_violation",
        ["Set Secure, HttpOnly and SameSite on cookies carrying personal data."],
    ),
    "DNC Registry Reference": (
        ["dnc", "marketing", "do_not_call", "spam"],
        "dnc_registry_violation",
        [
            "Check the DNC Registry before sending marketing messages to Singapore numbers.",
            "Publish an opt-out mechanism and honour requests within 30 days.",
        ],
    ),
    "Data Subject Rights Mechanism": (
        ["data_subject", "rights", "access_request", "correction", "withdrawal"],
        "data_subject_rights_violation",
        ["Publish a pathway for access, correction and consent-withdrawal requests."],
    ),
    "NRIC Exposure": (
        ["nric", "fin", "nric_collection", "nric_exposure", "identity document"],
        "nric_exposure_violation",
        [
            "Stop collecting full NRIC/FIN unless required by law (PDPC NRIC Guidelines).",
            "Remove exposed NRIC/FIN values from publicly reachable pages.",
        ],
    ),
    "Retention Limitation (§25)": (
        ["retention"],
        "retention_limitation_violation",
        [
            "Publish retention periods per data category.",
            "Cease retention once the purpose is served (PDPA §25).",
        ],
    ),
    "Data Breach Notification (§26B-D)": (
        ["breach", "notification", "notify"],
        "breach_notification_violation",
        [
            "Document a breach assessment and notification procedure.",
            "Notify PDPC within 3 calendar days of assessing a breach as notifiable.",
        ],
    ),
    "Cross-Border Transfer (§26)": (
        ["cross_border", "cross-border", "transfer"],
        "cross_border_transfer_violation",
        [
            "Map where personal data is hosted and processed.",
            "Put a transfer mechanism in place giving comparable protection (PDPA §26).",
        ],
    ),
}


def synthesize_dimension_finding(
    name: str, score: int, status: str, note: str = ""
) -> Optional[dict]:
    """Build one synthesized finding for a failing dimension, or None if unspecced.

    Shared by the Quick Scan PDF and the Cover Sheet so a dimension that fails
    produces the SAME finding text in both documents.
    """
    spec = DIMENSION_FINDING_SPEC.get(name)
    if not spec:
        return None
    _keywords, type_slug, requirements = spec
    severity = "HIGH" if status == "Non-Compliant" else "MEDIUM"

    # Every consumer that cites law reads `legislation`. A synthesized finding
    # omitting it is not merely unadorned: the PDPA Monitor briefing builds its
    # allow-list of citable sections from this key, so an empty one left the
    # customer-facing bullets reading "referencing PDPA section (none provided)".
    # Sourced from the curated VIOLATION_LEGISLATION map (imported lazily —
    # booppa_ai_service imports this module) so the wording can never diverge
    # from what the PDF prints for the same slug.
    try:
        from app.services.booppa_ai_service import VIOLATION_LEGISLATION
        legislation = "; ".join(VIOLATION_LEGISLATION.get(type_slug) or [])
    except Exception:  # pragma: no cover - import guard only
        legislation = ""

    return {
        "type": type_slug,
        "title": name,
        "severity": severity,
        "description": note or (
            f"Automated scan evidence scored this dimension {score}/100 ({status})."
        ),
        "evidence": (
            f"Automated scan evidence scored this dimension {score}/100 "
            f"({status}) — see Section 4."
        ),
        "requirements": requirements,
        "legislation": legislation,
        "acceptance_criteria": [
            f"A re-scan scores {name} at 85/100 or above (Compliant).",
        ],
        "deadline": "7 days" if severity == "HIGH" else "30 days",
        # Marks provenance: derived from the score table, not the AI.
        "derived_from_dimension": True,
    }


def spec_meta_for_type(type_slug: str) -> Optional[dict]:
    """Render metadata (title / requirements / acceptance criteria) for a spec slug.

    ``booppa_ai_service.VIOLATION_META`` only covers the six hand-written keyword
    checks. Without this, the dimension-derived violations render a §5 remediation
    task with an EMPTY body — telling a customer they have a gap and giving them
    nothing to do about it, which is its own kind of false document.

    Reads ``DIMENSION_FINDING_SPEC`` rather than duplicating the requirements into
    ``VIOLATION_META``, so the remediation text a buyer sees is identical whether
    it reached them via the Quick Scan PDF, the Cover Sheet, or this path.
    """
    for name, (_keywords, slug, requirements) in DIMENSION_FINDING_SPEC.items():
        if slug != type_slug:
            continue
        return {
            "title": name,
            "requirements": list(requirements),
            "acceptance_criteria": [
                f"A re-scan scores {name} at 85/100 or above (Compliant).",
            ],
        }
    return None


def _covered(existing: list, keywords: list[str]) -> bool:
    """True when some existing finding already speaks to this dimension."""
    for f in existing:
        if not isinstance(f, dict):
            continue
        blob = " ".join([
            (f.get("check_id") or ""),
            (f.get("type") or ""),
            (f.get("title") or ""),
        ]).lower()
        if any(k in blob for k in keywords):
            return True
    return False


def dimension_violations(assessment_data: Any, existing: list) -> list[dict]:
    """Detector-shaped violations for failing dimensions no existing one covers.

    This is the *upstream* half of the same fix as
    ``resolve_pdpa_findings_with_dimensions``. That function repairs documents at
    render time; this one repairs the source, so the violation exists before
    ``generate_compliance_report`` builds anything from it.

    That matters because ``violations`` is the input to far more than the
    findings list: the DeepSeek prompt (which only ever *describes* violations
    already detected — it cannot add one), ``calculate_risk_score``,
    ``_generate_recommendations``, ``_get_relevant_references`` and
    ``_generate_next_steps``. A dimension missing from this list is absent from
    all of them, no matter how good the scan evidence was.

    Returns the ``{type, severity, details, location, evidence}`` shape
    ``_detect_violations`` produces, so ``_generate_violation_detail`` can render
    it exactly like a natively-detected one.
    """
    from app.services.pdpa_dimension_snapshot import compute_dimension_snapshots

    ad = assessment_data if isinstance(assessment_data, dict) else {}
    site_url = ad.get("url") or ad.get("resolved_url") or "website"

    out: list[dict] = []
    for snap in compute_dimension_snapshots(ad):
        name = snap.get("dimension_name") or ""
        status = snap.get("status")
        if status not in FAILING_STATUSES:
            continue
        spec = DIMENSION_FINDING_SPEC.get(name)
        if not spec or _covered(existing, spec[0]):
            continue
        keywords, type_slug, requirements = spec
        score = int(snap.get("score", 0))
        out.append({
            "type": type_slug,
            "severity": "HIGH" if status == "Non-Compliant" else "MEDIUM",
            "details": (
                f"{name} scored {score}/100 ({status}) against {site_url} from "
                f"automated scan evidence. Required to remediate: "
                + " ".join(requirements)
            ),
            "location": site_url,
            "evidence": (
                f"Automated scan evidence scored {name} at {score}/100 ({status})."
            ),
            # Provenance: derived from the score table, not a keyword check.
            "derived_from_dimension": True,
        })

    if out:
        logger.info(
            "Detected %d violation(s) from failing dimensions with no keyword "
            "check behind them: %s",
            len(out),
            ", ".join(v["type"] for v in out),
        )
    return out


def resolve_pdpa_findings_with_dimensions(assessment_data: Any) -> list:
    """Findings the AI persisted, PLUS synthesized ones for failing dimensions.

    ``resolve_pdpa_findings`` returns only what the AI wrote, and
    ``_detect_violations`` never reads ``trackers`` / ``policy_clauses`` /
    ``hosting`` / ``pdpc_enforcement`` — so a scan can score several dimensions
    Non-Compliant while persisting zero findings. Every consumer that renders a
    findings list, a risk level, or a remediation section from that empty list
    then tells the customer they are clean. That is incident 22fb2871, and the
    Quick Scan PDF was only the first place it surfaced.

    Uses ``compute_dimension_snapshots``, which SKIPS any dimension it lacks
    evidence for. That is deliberate: it can only ever synthesize a finding for
    a dimension backed by real scan evidence, so it cannot manufacture a gap the
    score table does not also show.

    AI-authored findings always win — this only fills genuine gaps.
    """
    from app.services.pdpa_dimension_snapshot import compute_dimension_snapshots

    ad = assessment_data if isinstance(assessment_data, dict) else {}
    findings = resolve_pdpa_findings(ad)

    derived: list = []
    for snap in compute_dimension_snapshots(ad):
        name = snap.get("dimension_name")
        status = snap.get("status")
        if status not in FAILING_STATUSES:
            continue
        spec = DIMENSION_FINDING_SPEC.get(name or "")
        if not spec or _covered(findings, spec[0]):
            continue
        synth = synthesize_dimension_finding(name, int(snap.get("score", 0)), status)
        if synth:
            derived.append(synth)

    if derived:
        logger.info(
            "Synthesized %d finding(s) from failing dimensions with no AI finding: %s",
            len(derived),
            ", ".join(d["title"] for d in derived),
        )
    return list(findings) + derived


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


def latest_pdpa_score(
    db: Any,
    user_id: Any,
    domain: Optional[str] = None,
    allow_unattributed: bool = False,
) -> Optional[int]:
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

    ``allow_unattributed`` additionally accepts reports that recorded no
    ``company_website`` at all. ``ReportRequest.website`` is optional, so such
    rows exist and are not evidence of a *different* subject the way a
    contradicting hostname is — rejecting them would strand a vendor whose only
    scan happens to predate/omit the field. Use it for account-scoped vendor
    deliverables (see :func:`vendor_pdpa_score`); leave it off for certified
    per-report documents, where an unattributable score must not be claimed.
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
            # Unscoped keeps the original 5-row window. Scoped looks further
            # back: the filter is what narrows the result, so a small window
            # would let five scans of *other* companies hide this subject's
            # own report and return None.
            .limit(25 if domain else 5)
            .all()
        )
        want_host = _normalise_host(domain)
        for report in reports:
            if want_host:
                got_host = _normalise_host(report.company_website)
                if got_host != want_host and not (got_host is None and allow_unattributed):
                    continue
            score = resolve_pdpa_score(report.assessment_data)
            if score is not None:
                return score
        return None
    except Exception as exc:  # noqa: BLE001 — never block declaration on this
        logger.warning("latest_pdpa_score lookup failed for %s: %s", user_id, exc)
        return None


def vendor_pdpa_score(db: Any, user: Any) -> Optional[int]:
    """The PDPA score for a vendor's *own* company, for vendor-account products.

    Vendor Active/Pro deliverables (status snapshot, monthly digest, Pro report,
    proof certificate) are about the vendor's own business, but they used to read
    the account-wide latest score. A vendor account that also scans clients or
    prospects would render another company's number under its own letterhead —
    the account-cache contamination ``latest_pdpa_score`` warns about, on the
    exact products that print a company name beside the figure.

    Scoping is by ``User.website``. A report whose recorded website *contradicts*
    it is a different subject and is rejected; one with no recorded website is
    merely unattributed and is still accepted, so vendors whose scan omitted the
    optional website field keep their score. With no ``User.website`` there is
    nothing to scope by and the legacy account-wide lookup stands — no worse than
    today, and still ``None`` (→ "not assessed") when there is no scan at all.
    """
    if user is None:
        return None
    site = getattr(user, "website", None) or getattr(user, "website_hint", None)
    user_id = getattr(user, "id", None)
    if not site:
        return latest_pdpa_score(db, user_id)
    return latest_pdpa_score(db, user_id, domain=site, allow_unattributed=True)
