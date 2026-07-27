"""Absence of evidence must not render as evidence of compliance.

The tracker inventory is built by matching request URLs against a fixed list of
vendor signatures. An empty inventory was scored 95/Compliant with the note "No
third-party trackers observed during page load" — but that conflates two very
different states:

  * 180 requests captured, none matched a signature  -> real evidence
  * 0 requests captured, the capture failed          -> no evidence at all

and it overclaims even in the first case, because a tracker outside the
signature set is invisible to the check. This is the inverse of incident
22fb2871 and the more damaging direction: telling a customer they are clean on a
dimension we did not establish.
"""
import pytest

from app.services.pdf_service import PDFService
from app.services.pdpa_dimension_snapshot import (
    compute_dimension_snapshots,
    tracker_capture_is_usable,
)

_GOOD = {"inventory": [], "total_requests_captured": 180, "signature_count": 62}
_FAILED = {"inventory": [], "total_requests_captured": 0, "signature_count": 62}
_HITS = {"inventory": ["Google Analytics"], "total_requests_captured": 40}


def _dims(scan):
    computed = PDFService()._compute_dimension_scores([], scan)
    return {d[0]: d for d in computed["dimensions"]}, computed["not_assessed"]


# ── The gate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("trackers,usable", [
    (_GOOD, True),
    (_FAILED, False),
    (_HITS, True),
    # Legacy rows predate the counter — do not retroactively flip old reports.
    ({"inventory": []}, True),
    ({"inventory": [], "total_requests_captured": "junk"}, True),
    (None, False),
])
def test_capture_usability_gate(trackers, usable):
    assert tracker_capture_is_usable(trackers) is usable


# ── PDF score table ─────────────────────────────────────────────────────────


def test_failed_capture_is_not_assessed_not_compliant():
    dims, not_assessed = _dims({"trackers": _FAILED})
    name = "Third-Party Tracker Inventory"

    assert dims[name][2] == "Not Assessed"
    assert name in not_assessed
    assert "no conclusion" in dims[name][3].lower()


def _overall(scan):
    PDFService()._compute_dimension_scores([], scan)
    return scan["computed_overall_compliance_score"]


def test_failed_capture_scores_as_if_the_dimension_were_absent():
    """A failed capture must contribute nothing to the headline number — the
    same as a scan that never attempted the check at all."""
    assert _overall({"trackers": _FAILED}) == _overall({})


def test_an_assessed_tracker_dimension_does_move_the_score():
    """Guards the above from being vacuous: the dimension must carry real weight
    when it IS assessed, otherwise 'contributes nothing' proves nothing."""
    assert _overall({"trackers": _GOOD}) != _overall({"trackers": _HITS})


def test_real_clean_result_states_what_was_actually_checked():
    dims, not_assessed = _dims({"trackers": _GOOD})
    name = "Third-Party Tracker Inventory"
    note = dims[name][3]

    assert dims[name][2] == "Compliant"
    assert name not in not_assessed
    # Must name the scope of the check, not assert universal absence.
    assert "62" in note and "180" in note
    assert "signature" in note.lower()
    assert "No third-party trackers observed during page load." != note


def test_trackers_found_still_reads_as_non_compliant():
    """The fix must not soften a genuine finding."""
    dims, _ = _dims({"trackers": _HITS})
    d = dims["Third-Party Tracker Inventory"]
    assert d[2] == "Non-Compliant"
    assert "Google Analytics" in d[3]


def test_scan_that_never_ran_is_still_distinguished_from_one_that_failed():
    no_run, _ = _dims({})
    failed, _ = _dims({"trackers": _FAILED})
    assert no_run["Third-Party Tracker Inventory"][3] != failed["Third-Party Tracker Inventory"][3]


# ── Snapshot engine ─────────────────────────────────────────────────────────


def test_snapshot_skips_the_dimension_on_a_failed_capture():
    names = {s["dimension_name"] for s in compute_dimension_snapshots({"trackers": _FAILED})}
    assert "Third-Party Tracker Inventory" not in names


def test_snapshot_still_scores_a_usable_capture():
    snaps = {s["dimension_name"]: s for s in compute_dimension_snapshots({"trackers": _GOOD})}
    assert snaps["Third-Party Tracker Inventory"]["status"] == "Compliant"


def test_failed_capture_alone_does_not_produce_a_cookie_verdict():
    """Previously a failed capture with no consent data fell through to score 25
    — a Non-Compliant verdict resting on nothing."""
    names = {s["dimension_name"] for s in compute_dimension_snapshots({"trackers": _FAILED})}
    assert "Cookie Consent Mechanism" not in names


def test_consent_evidence_still_scores_when_capture_failed():
    """A failed tracker capture must not suppress a verdict the consent scan
    genuinely supports."""
    snaps = {
        s["dimension_name"]: s
        for s in compute_dimension_snapshots({
            "trackers": _FAILED,
            "consent_mechanism": {"has_cookie_banner": True},
        })
    }
    assert snaps["Cookie Consent Mechanism"]["status"] == "Compliant"


def test_failed_capture_cannot_synthesize_a_tracker_finding():
    """End to end: a failed capture must not reach the customer as a finding in
    any direction."""
    from app.services.pdpa_findings import resolve_pdpa_findings_with_dimensions

    out = resolve_pdpa_findings_with_dimensions({"trackers": _FAILED})
    assert out == []


# ── Deduplicated indicator list ─────────────────────────────────────────────


def test_cookie_indicators_have_a_single_definition():
    import inspect

    from app.workers import tasks

    src = inspect.getsource(tasks)
    assert len(tasks.COOKIE_BANNER_INDICATORS) == 29
    # The literal must appear exactly once — in the constant itself.
    assert src.count('"consentmanager"') == 1


def test_trm_gap_analysis_requests_json_mode():
    import inspect

    from app import trm_workflow_service

    src = inspect.getsource(trm_workflow_service)
    assert "json_mode=True" in src
