"""
Stable Finding Keys
===================
Same finding across two different scans MUST produce the same key, otherwise
the remediation confirmation logic in `process_report_workflow` cannot tell
"the user fixed it" apart from "we never saw this finding before."

Keys are derived from finding *types*, not from finding *content*. A new
NRIC field appearing on a different page is still the same kind of NRIC
exposure for remediation purposes. The user who marked it fixed cares
that the type is gone, not that one specific URL no longer matches.

Source-of-truth mapping (each line = one stable key):

  free_scan check_id            →  free:<check_id>
  pdpa dimension status flip    →  dim:<dim_slug>
  policy_clauses missing item   →  clause:<clause_name>
  nric kind (collection/leak)   →  nric:<kind>
  tracker inventory vendor      →  tracker:<vendor_slug>
  pdpc enforcement hit          →  breach:pdpc_enforcement
  hosting non-singapore         →  xbt:non_sg
"""
from __future__ import annotations


import re
from typing import Any, Iterable


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w]+", "_", s)
    return s.strip("_")[:64]


def extract_finding_keys(assessment_data: dict[str, Any] | None) -> set[str]:
    """Return the set of stable finding keys currently present in this scan.

    Used by both the API (so a user can mark only findings that *actually*
    exist in their current report) and the auto-confirmation loop (so it can
    check whether a previously-marked finding is gone).
    """
    sd = assessment_data or {}
    keys: set[str] = set()

    # ── Free-scan style findings (already carry check_id) ──────────────
    for f in sd.get("findings", []) or []:
        check_id = f.get("check_id") if isinstance(f, dict) else None
        if check_id:
            keys.add(f"free:{_slug(check_id)}")

    # ── NRIC ───────────────────────────────────────────────────────────
    nric = sd.get("nric") if isinstance(sd.get("nric"), dict) else None
    if nric:
        kind = nric.get("kind") or "none"
        if kind in {"collection", "leakage"}:
            keys.add(f"nric:{kind}")

    # ── Policy clauses (one key per missing clause) ────────────────────
    clauses = sd.get("policy_clauses") if isinstance(sd.get("policy_clauses"), dict) else None
    if clauses:
        for missing in clauses.get("missing") or []:
            keys.add(f"clause:{_slug(missing)}")

    # ── PDPC enforcement (breach signal) ───────────────────────────────
    pdpc = sd.get("pdpc_enforcement") if isinstance(sd.get("pdpc_enforcement"), dict) else None
    if pdpc and pdpc.get("checked") and pdpc.get("found"):
        keys.add("breach:pdpc_enforcement")

    # ── Cross-border transfer (non-SG hosting) ─────────────────────────
    hosting = sd.get("hosting") if isinstance(sd.get("hosting"), dict) else None
    if hosting and hosting.get("checked"):
        region = hosting.get("inferred_region")
        if region != "Singapore" and hosting.get("inferred_provider"):
            keys.add("xbt:non_sg")

    # ── Trackers (one key per tracker vendor present pre-consent) ──────
    trackers = sd.get("trackers") if isinstance(sd.get("trackers"), dict) else None
    if trackers:
        for vendor in trackers.get("inventory") or []:
            keys.add(f"tracker:{_slug(vendor)}")

    # ── Consent mechanism missing ──────────────────────────────────────
    consent = sd.get("consent_mechanism") if isinstance(sd.get("consent_mechanism"), dict) else None
    if consent is not None and consent.get("has_cookie_banner") is False:
        keys.add("dim:cookie_consent_missing")

    # ── DPO contact missing ────────────────────────────────────────────
    dpo = sd.get("dpo_compliance") if isinstance(sd.get("dpo_compliance"), dict) else None
    if dpo is not None and not dpo.get("has_dpo"):
        keys.add("dim:dpo_missing")

    # ── Privacy policy link missing ────────────────────────────────────
    pp = sd.get("privacy_policy") if isinstance(sd.get("privacy_policy"), dict) else None
    if pp is not None and not pp.get("found"):
        keys.add("dim:privacy_policy_missing")

    return keys


# ── Human-readable labels for the API + PDF + email ───────────────────────────

_STATIC_LABELS = {
    "nric:collection": "NRIC collection detected",
    "nric:leakage": "NRIC leakage on public pages",
    "breach:pdpc_enforcement": "PDPC enforcement action on record",
    "xbt:non_sg": "Cross-border data transfer signal",
    "dim:cookie_consent_missing": "Cookie consent mechanism missing",
    "dim:dpo_missing": "DPO contact not publicly disclosed",
    "dim:privacy_policy_missing": "Privacy policy link missing",
}

_CLAUSE_LABELS = {
    "purpose": "Privacy policy: Purpose of collection missing",
    "withdrawal": "Privacy policy: Consent withdrawal mechanism missing",
    "dpo_contact": "Privacy policy: DPO contact missing",
    "retention": "Privacy policy: Retention period missing",
    "third_party": "Privacy policy: Third-party transfer disclosure missing",
    "data_subject_rights": "Privacy policy: Access & correction rights missing",
}


def label_for_key(key: str) -> str:
    """Return a short human-readable description for a finding key."""
    if key in _STATIC_LABELS:
        return _STATIC_LABELS[key]
    if key.startswith("clause:"):
        clause = key.split(":", 1)[1]
        return _CLAUSE_LABELS.get(clause, f"Privacy policy clause missing: {clause}")
    if key.startswith("tracker:"):
        vendor = key.split(":", 1)[1].replace("_", " ").title()
        return f"Pre-consent third-party tracker: {vendor}"
    if key.startswith("free:"):
        check_id = key.split(":", 1)[1].replace("_", " ")
        return f"Finding: {check_id}"
    return key


def is_key_present(assessment_data: dict[str, Any] | None, key: str) -> bool:
    """Whether the given finding key is present in this scan's data."""
    return key in extract_finding_keys(assessment_data)
