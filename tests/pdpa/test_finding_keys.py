"""Tests for app.services.finding_keys.

The contract that matters most: the same finding across two different scans
MUST produce the same key. If this breaks, remediation auto-confirmation
breaks silently — users will see their fixes never confirm.
"""
import pytest

from app.services.finding_keys import (
    extract_finding_keys,
    is_key_present,
    label_for_key,
)


SCAN_RICH = {
    "findings": [
        {"check_id": "no_dpo_contact", "severity": "MEDIUM"},
        {"check_id": "https", "severity": "CRITICAL"},
    ],
    "nric": {"kind": "leakage", "status": "Non-Compliant"},
    "policy_clauses": {"missing": ["retention", "withdrawal"]},
    "pdpc_enforcement": {"checked": True, "found": True},
    "hosting": {"checked": True, "inferred_provider": "AWS", "inferred_region": None},
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel"]},
    "consent_mechanism": {"has_cookie_banner": False},
    "dpo_compliance": {"has_dpo": False},
    "privacy_policy": {"found": False},
}


class TestExtract:
    def test_extracts_all_known_categories(self):
        keys = extract_finding_keys(SCAN_RICH)
        # Free-scan style
        assert "free:no_dpo_contact" in keys
        assert "free:https" in keys
        # NRIC
        assert "nric:leakage" in keys
        # Clauses (one per missing)
        assert "clause:retention" in keys
        assert "clause:withdrawal" in keys
        # PDPC enforcement
        assert "breach:pdpc_enforcement" in keys
        # Cross-border
        assert "xbt:non_sg" in keys
        # Trackers (slugged)
        assert "tracker:google_analytics" in keys
        assert "tracker:meta_pixel" in keys
        # Dimension misses
        assert "dim:cookie_consent_missing" in keys
        assert "dim:dpo_missing" in keys
        assert "dim:privacy_policy_missing" in keys

    def test_empty_input(self):
        assert extract_finding_keys(None) == set()
        assert extract_finding_keys({}) == set()

    def test_compliant_scan_returns_empty(self):
        sd = {
            "nric": {"kind": "none"},
            "policy_clauses": {"missing": []},
            "pdpc_enforcement": {"checked": True, "found": False},
            "hosting": {"checked": True, "inferred_provider": "AWS",
                        "inferred_region": "Singapore"},
            "trackers": {"inventory": []},
            "consent_mechanism": {"has_cookie_banner": True},
            "dpo_compliance": {"has_dpo": True},
            "privacy_policy": {"found": True},
        }
        assert extract_finding_keys(sd) == set()


class TestStability:
    """Same logical finding across scans → same key. The whole remediation
    flow depends on this being true."""

    def test_same_nric_kind_yields_same_key(self):
        s1 = {"nric": {"kind": "leakage"}}
        s2 = {"nric": {"kind": "leakage"}}  # different scan, same finding type
        assert extract_finding_keys(s1) == extract_finding_keys(s2)

    def test_tracker_vendor_capitalisation_normalised(self):
        s1 = {"trackers": {"inventory": ["Google Analytics"]}}
        s2 = {"trackers": {"inventory": ["google analytics"]}}
        assert extract_finding_keys(s1) == extract_finding_keys(s2)

    def test_check_id_underscore_versus_dash_normalised(self):
        s1 = {"findings": [{"check_id": "no_dpo_contact"}]}
        s2 = {"findings": [{"check_id": "no-dpo-contact"}]}
        assert extract_finding_keys(s1) == extract_finding_keys(s2)


class TestIsKeyPresent:
    def test_present(self):
        assert is_key_present(SCAN_RICH, "nric:leakage")
        assert is_key_present(SCAN_RICH, "tracker:google_analytics")

    def test_absent(self):
        assert not is_key_present(SCAN_RICH, "nric:collection")
        assert not is_key_present({}, "anything")


class TestLabelForKey:
    def test_known_static_labels(self):
        assert "NRIC leakage" in label_for_key("nric:leakage")
        assert "PDPC enforcement" in label_for_key("breach:pdpc_enforcement")

    def test_clause_label_resolution(self):
        assert "Retention" in label_for_key("clause:retention")
        assert "withdrawal" in label_for_key("clause:withdrawal").lower()

    def test_tracker_label_resolution(self):
        assert "Google Analytics" in label_for_key("tracker:google_analytics")

    def test_unknown_falls_back_to_key(self):
        assert label_for_key("zzz:unknown") == "zzz:unknown"
