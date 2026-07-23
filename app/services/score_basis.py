"""Score provenance — what actually drove each Deep Scan dimension score.

Layer-3 backlog items L3-1 and L3-2 from `docs/layer3_audit_pdpa_rfp_vendor_scores.md`.

A vendor score is only defensible to a regulator or a procurement officer if the
buyer can see *why* each dimension scored the way it did, and whether the basis
was an inference from a public signal or evidence that was actually tested. The
driving signals were already computed and persisted (`deep_scan_service._dim`
writes them to `DeepScanDimensionHistory.detail`) — nothing rendered them.

Two things happen here and nothing else:

  1. `detail` is turned into a plain-English basis line ("DPO named on public
     site"), so a number becomes an argument.
  2. Each dimension is annotated `Inferred (public scan)` or `Tested — <date>`,
     the latter only when a TRM control in the mapped domain carries a
     `TrmEvidence` row with `evidence_type="tested"`. MAS treats an untested
     plan as an aspiration, not a control; the same distinction applies here.

Deliberately *not* done: tested evidence does not change the numeric score. That
is a pricing/consistency decision, not a rendering one. Annotation only.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

BASIS_INFERRED = "Inferred (public scan)"

# Deep Scan dimension → MAS TRM domain (`app.core.models.MAS_TRM_DOMAINS`).
# Deliberately partial and conservative: a dimension only maps where tested
# evidence in that domain would genuinely bear on the dimension. Anything
# unmapped stays `Inferred`, which is the honest answer — a mapping invented to
# fill the table would let unrelated evidence dress up a score.
DIMENSION_TRM_DOMAIN: dict[str, str] = {
    "Access & Correction (§21/22)": "Data and Information Management",
    "Consent Obligation (§13/14)": "Data and Information Management",
    "Retention Limitation (§25)": "Data and Information Management",
    "Transfer Limitation (§26)": "Cloud Computing",
    "Protection Obligation (§24)": "Cyber Security",
    "Data Breach Notification (§26A-D)": "Incident Management",
    "Sub-Processor & DPA Disclosure": "IT Outsourcing and Vendor Management",
    "NRIC / Identifier Handling": "Authentication and Access Management",
}

# `detail` key → (phrase when true/present, phrase when false/absent).
# `deep_scan_service` writes a small closed set of keys; anything outside it
# falls through to a generic "key = value" rendering rather than being dropped.
_SIGNAL_PHRASES: dict[str, tuple[str, str]] = {
    "pdpa_mentioned": ("PDPA referenced on public site", "no PDPA reference found on public site"),
    "dpo_mentioned": ("DPO named on public site", "no DPO named on public site"),
    "dpa_mentioned": ("data-processing agreement referenced", "no data-processing agreement referenced"),
    "retention_policy_mentioned": ("retention policy published", "no retention policy published"),
    "breach_policy_mentioned": ("breach-response policy published", "no breach-response policy published"),
    "subprocessors": ("sub-processors disclosed", "sub-processors not disclosed"),
    "dpa": ("DPA disclosed", "DPA not disclosed"),
    "residency_stated": ("Singapore data residency stated", "data residency not stated"),
    "pdpc_enforcement_found": ("published PDPC enforcement action found", "no published PDPC enforcement action"),
    "pdpc_signal": ("published PDPC enforcement action found", "no published PDPC enforcement action"),
    "pdpc_enforcement": ("published PDPC enforcement action found", ""),
}


def _phrase(key: str, value: Any) -> str:
    """Render one `detail` entry as a clause a buyer can read."""
    if key in _SIGNAL_PHRASES:
        yes, no = _SIGNAL_PHRASES[key]
        return yes if value else no
    if key == "cookie_flags":
        n = int(value or 0)
        return "no cookie-consent findings" if n == 0 else f"{n} cookie-consent finding(s)"
    if key == "policy_signals":
        return f"{int(value or 0)} of 4 governance policies published"
    if key == "ssl_grade":
        return f"TLS grade {value}" if value else "TLS grade not retrievable"
    if key == "high_findings":
        n = int(value or 0)
        return "no high/critical scan findings" if n == 0 else f"{n} high/critical scan finding(s)"
    if key == "inferred_region":
        return f"hosting inferred in {value}" if value else "hosting region not inferable"
    if key == "held":
        items = list(value or [])
        return ("certifications published: " + ", ".join(items)) if items else "no certifications published"
    if key == "iso_27001_year":
        return f"ISO 27001 dated {value}" if value else ""
    if key == "acra_status":
        return "entity not found in ACRA" if value == "not_found" else f"ACRA entity status: {value}"
    if key == "entity_age_years":
        return f"entity registered {value} year(s)"
    if key == "gebiz_award_count":
        n = int(value or 0)
        return "no GeBIZ awards on record" if n == 0 else f"{n} GeBIZ award(s) on record"
    if key == "days_since_last_award":
        return f"last GeBIZ award {value} day(s) ago"
    return f"{key.replace('_', ' ')} = {value}"


def describe_detail(detail: dict | None) -> str:
    """Plain-English rendering of a dimension's persisted driving signals."""
    if not isinstance(detail, dict) or not detail:
        return "No signal recorded for this dimension."
    parts = [p for p in (_phrase(k, v) for k, v in detail.items()) if p]
    if not parts:
        return "No signal recorded for this dimension."
    text = "; ".join(parts)
    return text[0].upper() + text[1:]


def _tested_domains(db, vendor_id) -> dict[str, Any]:
    """MAS TRM domain → most recent `tested_at` across that domain's evidence.

    Scoped to organisations the vendor owns. Returns {} on any failure — score
    provenance must never be the reason a paid report fails to render.
    """
    try:
        from app.core.models import Organisation, TrmControl, TrmEvidence

        rows = (
            db.query(TrmControl.domain, TrmEvidence.tested_at)
            .join(TrmEvidence, TrmEvidence.control_id == TrmControl.id)
            .join(Organisation, Organisation.id == TrmControl.organisation_id)
            .filter(
                Organisation.owner_user_id == vendor_id,
                TrmEvidence.evidence_type == "tested",
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001 — provenance is additive, never fatal
        logger.info("Score-basis tested-evidence lookup failed for %s: %s", vendor_id, e)
        return {}

    out: dict[str, Any] = {}
    for domain, tested_at in rows:
        if not domain:
            continue
        prior = out.get(domain)
        if prior is None or (tested_at and tested_at > prior):
            out[domain] = tested_at
    return out


def build_score_basis(db, vendor_id) -> list[dict[str, Any]]:
    """One provenance row per Deep Scan dimension, newest scan.

    Returns [] when the vendor has no persisted Deep Scan — callers skip the
    section rather than printing an empty table.
    """
    from app.services.deep_scan_service import latest_deep_scan

    try:
        scan = latest_deep_scan(db, vendor_id)
    except Exception as e:  # noqa: BLE001
        logger.info("Score-basis Deep Scan load failed for %s: %s", vendor_id, e)
        return []
    if not scan or not scan.get("dimensions"):
        return []

    tested = _tested_domains(db, vendor_id)

    rows: list[dict[str, Any]] = []
    for d in scan["dimensions"]:
        name = d.get("dimension_name") or ""
        domain = DIMENSION_TRM_DOMAIN.get(name)
        basis = BASIS_INFERRED
        if domain and domain in tested:
            when = tested[domain]
            basis = f"Tested — {when.strftime('%d %b %Y')}" if when else "Tested"
        rows.append({
            "dimension_name": name,
            "category": d.get("category") or "pdpa",
            "status": d.get("status") or "—",
            "score": d.get("score"),
            "signal": describe_detail(d.get("detail")),
            "basis": basis,
            "trm_domain": domain,
        })
    return rows
