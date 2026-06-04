"""
PDPA Dimension Snapshot
=======================
Derives per-dimension (name, status, score) tuples from a worker's
`assessment_data` dict. Used by:

  1. `process_report_workflow` to write rows into `pdpa_dimension_history`
     after a scan completes.
  2. `compliance_drift.detect_drift_for_vendor` to compare current vs
     previous snapshots and detect dimension-level Compliant → Non-Compliant
     flips.

This is intentionally separate from `pdf_service._compliance_score_table`
because we don't want the history layer to depend on PDF rendering. Both
modules read the same `assessment_data` keys.
"""

from __future__ import annotations

from typing import Any


def _classify(score: int) -> str:
    if score >= 85:
        return "Compliant"
    if score >= 50:
        return "Partial"
    return "Non-Compliant"


def compute_dimension_snapshots(assessment_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return one snapshot per high-signal PDPA dimension.

    Skips dimensions we lack the data to assess (no false 'Compliant' rows).
    Each snapshot: {dimension_name, status, score}.
    """
    sd = assessment_data or {}
    out: list[dict[str, Any]] = []

    # ── NRIC Exposure ─────────────────────────────────────────────────────
    nric = sd.get("nric") if isinstance(sd.get("nric"), dict) else None
    if nric:
        out.append({
            "dimension_name": "NRIC Exposure",
            "status": nric.get("status", "Compliant"),
            "score": int(nric.get("score", 100)),
        })

    # ── Privacy Policy (§11/13) + Retention (§25) — from clause classifier
    clauses = sd.get("policy_clauses") if isinstance(sd.get("policy_clauses"), dict) else None
    if clauses:
        out.append({
            "dimension_name": "Privacy Policy (PDPA §11/13)",
            "status": clauses.get("status", "Non-Compliant"),
            "score": int(clauses.get("score", 0)),
        })
        items = clauses.get("items") or []
        retention_v = next((i for i in items if i.get("clause") == "retention"), None)
        if retention_v:
            ret_score = 92 if retention_v.get("present") else 20
            out.append({
                "dimension_name": "Retention Limitation (§25)",
                "status": _classify(ret_score),
                "score": ret_score,
            })

    # ── Data Breach Notification (§26B-D) — from PDPC enforcement check ──
    pdpc = sd.get("pdpc_enforcement") if isinstance(sd.get("pdpc_enforcement"), dict) else None
    if pdpc and pdpc.get("checked"):
        score = 10 if pdpc.get("found") else 95
        out.append({
            "dimension_name": "Data Breach Notification (§26B-D)",
            "status": _classify(score),
            "score": score,
        })

    # ── Cross-Border Transfer (§26) — from hosting inference ─────────────
    hosting = sd.get("hosting") if isinstance(sd.get("hosting"), dict) else None
    if hosting and hosting.get("checked"):
        if hosting.get("inferred_region") == "Singapore":
            score = 92
        elif hosting.get("inferred_provider"):
            score = 60
        else:
            score = 75
        out.append({
            "dimension_name": "Cross-Border Transfer (§26)",
            "status": _classify(score),
            "score": score,
        })

    # ── Third-Party Tracker Inventory ─────────────────────────────────────
    trackers = sd.get("trackers") if isinstance(sd.get("trackers"), dict) else None
    if trackers:
        inventory = trackers.get("inventory") or []
        score = 30 if inventory else 95
        out.append({
            "dimension_name": "Third-Party Tracker Inventory",
            "status": _classify(score),
            "score": score,
        })

    # ── Cookie Consent (behaviour-aware when trackers data present) ──────
    consent = sd.get("consent_mechanism") if isinstance(sd.get("consent_mechanism"), dict) else None
    if consent or trackers:
        if trackers and (trackers.get("inventory") or []):
            score = 8
        elif consent and consent.get("has_cookie_banner"):
            score = 96
        else:
            score = 25
        out.append({
            "dimension_name": "Cookie Consent Mechanism",
            "status": _classify(score),
            "score": score,
        })

    return out


def diff_snapshots(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return per-dimension transitions where status WORSENED.

    A worsening = Compliant → Partial/Non-Compliant, or Partial → Non-Compliant.
    Improvements and unchanged dimensions are not surfaced.
    """
    rank = {"Compliant": 0, "Partial": 1, "Non-Compliant": 2}
    prev_by_dim = {s["dimension_name"]: s for s in previous}
    out: list[dict[str, Any]] = []
    for cur in current:
        prev = prev_by_dim.get(cur["dimension_name"])
        if not prev:
            continue
        if rank.get(cur["status"], 0) > rank.get(prev["status"], 0):
            out.append({
                "dimension_name": cur["dimension_name"],
                "previous_status": prev["status"],
                "current_status": cur["status"],
                "previous_score": prev["score"],
                "current_score": cur["score"],
            })
    return out
