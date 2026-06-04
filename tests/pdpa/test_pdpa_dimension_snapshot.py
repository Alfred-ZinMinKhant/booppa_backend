"""Tests for app.services.pdpa_dimension_snapshot.

compute_dimension_snapshots() must:
  - skip dimensions we lack data for (no false 'Compliant' rows)
  - never produce duplicate dimension names
  - reflect the same status/score logic the PDF report uses

diff_snapshots() must:
  - return ONLY worsening transitions
  - ignore improvements and unchanged dimensions
"""
import pytest

from app.services.pdpa_dimension_snapshot import (
    compute_dimension_snapshots,
    diff_snapshots,
)


SCAN_DATA_CLEAN = {
    "nric": {"status": "Compliant", "score": 100, "kind": "none"},
    "policy_clauses": {
        "status": "Compliant", "score": 100,
        "present_count": 6, "total": 6, "missing": [],
        "items": [{"clause": "retention", "present": True}],
    },
    "pdpc_enforcement": {"checked": True, "found": False, "cases": []},
    "hosting": {"checked": True, "inferred_provider": "AWS", "inferred_region": "Singapore"},
    "trackers": {"inventory": [], "pre_consent": [], "post_consent": []},
    "consent_mechanism": {"has_cookie_banner": True, "detected_providers": ["onetrust"]},
}

SCAN_DATA_DEGRADED = {
    **SCAN_DATA_CLEAN,
    "pdpc_enforcement": {"checked": True, "found": True, "cases": [{"title": "X"}]},
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel"],
                 "pre_consent": [{"vendor": "GA", "sample_url": "x", "count": 1}],
                 "post_consent": []},
}


class TestComputeSnapshots:
    def test_clean_data_produces_seven_compliant_snapshots(self):
        snaps = compute_dimension_snapshots(SCAN_DATA_CLEAN)
        assert len(snaps) == 7
        assert all(s["status"] == "Compliant" for s in snaps)

    def test_missing_data_skips_dimensions(self):
        # Only nric + hosting in scan data → only 2 snapshots
        sd = {
            "nric": {"status": "Compliant", "score": 100, "kind": "none"},
            "hosting": {"checked": True, "inferred_provider": "AWS", "inferred_region": "Singapore"},
        }
        snaps = compute_dimension_snapshots(sd)
        names = {s["dimension_name"] for s in snaps}
        assert names == {"NRIC Exposure", "Cross-Border Transfer (§26)"}

    def test_dimension_names_unique(self):
        snaps = compute_dimension_snapshots(SCAN_DATA_DEGRADED)
        names = [s["dimension_name"] for s in snaps]
        assert len(names) == len(set(names))

    def test_degraded_data_marks_breach_and_trackers_non_compliant(self):
        snaps = compute_dimension_snapshots(SCAN_DATA_DEGRADED)
        by_name = {s["dimension_name"]: s for s in snaps}
        assert by_name["Data Breach Notification (§26B-D)"]["status"] == "Non-Compliant"
        assert by_name["Third-Party Tracker Inventory"]["status"] == "Non-Compliant"
        assert by_name["Cookie Consent Mechanism"]["status"] == "Non-Compliant"

    def test_none_input(self):
        assert compute_dimension_snapshots(None) == []


class TestDiff:
    def test_clean_to_degraded_yields_three_flips(self):
        previous = compute_dimension_snapshots(SCAN_DATA_CLEAN)
        current = compute_dimension_snapshots(SCAN_DATA_DEGRADED)
        flips = diff_snapshots(previous, current)
        flip_names = {f["dimension_name"] for f in flips}
        assert "Data Breach Notification (§26B-D)" in flip_names
        assert "Third-Party Tracker Inventory" in flip_names
        assert "Cookie Consent Mechanism" in flip_names

    def test_no_change_yields_empty(self):
        snaps = compute_dimension_snapshots(SCAN_DATA_CLEAN)
        assert diff_snapshots(snaps, snaps) == []

    def test_improvement_not_surfaced(self):
        prev = compute_dimension_snapshots(SCAN_DATA_DEGRADED)
        cur = compute_dimension_snapshots(SCAN_DATA_CLEAN)
        flips = diff_snapshots(prev, cur)
        # Going Non-Compliant → Compliant is NOT a flip we surface
        assert flips == []

    def test_missing_dimension_in_previous_skipped(self):
        previous = [
            {"dimension_name": "Existing", "status": "Compliant", "score": 100},
        ]
        current = [
            {"dimension_name": "Existing", "status": "Compliant", "score": 100},
            {"dimension_name": "NewDim", "status": "Non-Compliant", "score": 0},
        ]
        # New dimension has no previous baseline; do not flag as flip
        assert diff_snapshots(previous, current) == []

    def test_partial_to_non_compliant_is_flip(self):
        prev = [{"dimension_name": "X", "status": "Partial", "score": 60}]
        cur = [{"dimension_name": "X", "status": "Non-Compliant", "score": 20}]
        flips = diff_snapshots(prev, cur)
        assert len(flips) == 1
        assert flips[0]["previous_status"] == "Partial"
        assert flips[0]["current_status"] == "Non-Compliant"
