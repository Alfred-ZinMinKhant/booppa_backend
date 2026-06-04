"""Tests for app.services.nric_classifier.

Verifies the checksum-validated value detector, snippet harvesting + redaction,
the heuristic (no-LLM) classification fallback, and the dimension roll-up.
"""
import asyncio

import pytest

from app.services.nric_classifier import (
    _is_valid_nric_checksum,
    _redact_nric,
    classify_candidates,
    find_valid_nric_values,
    harvest_candidates,
    summarise,
)


class TestChecksum:
    def test_known_good_singapore_nric_passes(self):
        # S1234567D is a published PDPC test vector.
        assert _is_valid_nric_checksum("S1234567D")

    def test_wrong_check_digit_fails(self):
        assert not _is_valid_nric_checksum("S1234567A")

    def test_invalid_prefix_fails(self):
        # Q is not a valid Singapore NRIC prefix
        assert not _is_valid_nric_checksum("Q1234567A")

    def test_short_input_fails(self):
        assert not _is_valid_nric_checksum("S12345")

    def test_lowercase_normalised(self):
        assert _is_valid_nric_checksum("s1234567d")


class TestRedaction:
    def test_redacts_valid_nric_inline(self):
        out = _redact_nric("Contact S1234567D about the form")
        assert "S1234567D" not in out
        assert "[REDACTED-NRIC]" in out

    def test_leaves_non_nric_alone(self):
        text = "No identifiers in this sentence at all."
        assert _redact_nric(text) == text


class TestHarvest:
    def test_returns_both_label_and_value_hits(self):
        html = (
            "<form><label>NRIC</label><input name=nric placeholder=\"S1234567D\"></form>"
            "<div>Customer S1234567D placed an order.</div>"
        )
        cands = harvest_candidates(html, source_url="https://x/")
        hints = {c["hint"] for c in cands}
        assert "label" in hints
        assert "value" in hints

    def test_snippets_never_contain_raw_nric(self):
        html = "Customer file: S1234567D contacted DPO."
        cands = harvest_candidates(html, source_url="https://x/")
        assert cands, "expected at least one candidate"
        for c in cands:
            assert "S1234567D" not in c["snippet"]

    def test_returns_empty_when_no_signals(self):
        cands = harvest_candidates("<p>Hello world</p>", source_url="https://x/")
        assert cands == []


class TestFindValidValues:
    def test_filters_to_valid_checksum_only(self):
        text = "Pair: S1234567D (valid) and S9999999Z (invalid)"
        assert find_valid_nric_values(text) == ["S1234567D"]

    def test_empty_string(self):
        assert find_valid_nric_values("") == []


class TestHeuristicClassifier:
    def test_value_hint_becomes_leakage(self):
        cands = [{"snippet": "Customer [REDACTED-NRIC] placed an order.",
                  "source_url": "u", "hint": "value"}]
        results = asyncio.run(classify_candidates(cands, provider=None))
        assert results[0].kind == "leakage"

    def test_form_input_becomes_collection(self):
        cands = [{"snippet": '<input name="nric" placeholder="enter your NRIC">',
                  "source_url": "u", "hint": "label"}]
        results = asyncio.run(classify_candidates(cands, provider=None))
        assert results[0].kind == "collection"

    def test_negation_becomes_policy_mention(self):
        cands = [{"snippet": "We do not collect NRIC numbers from website visitors.",
                  "source_url": "u", "hint": "label"}]
        results = asyncio.run(classify_candidates(cands, provider=None))
        assert results[0].kind == "policy_mention"


class TestSummarise:
    def _ev(self, kind):
        from app.services.nric_classifier import NricEvidence
        return NricEvidence(kind=kind, snippet="x", source_url="u",
                            confidence=0.7, note="")

    def test_leakage_dominates(self):
        s = summarise([self._ev("policy_mention"), self._ev("leakage")])
        assert s["kind"] == "leakage"
        assert s["status"] == "Non-Compliant"
        assert s["score"] == 0

    def test_collection_when_no_leakage(self):
        s = summarise([self._ev("policy_mention"), self._ev("collection")])
        assert s["kind"] == "collection"
        assert s["status"] == "Non-Compliant"

    def test_policy_mentions_only_compliant(self):
        s = summarise([self._ev("policy_mention"), self._ev("policy_mention")])
        assert s["kind"] == "policy_mention"
        assert s["status"] == "Compliant"

    def test_no_evidence_compliant(self):
        s = summarise([])
        assert s["kind"] == "none"
        assert s["status"] == "Compliant"
        assert s["score"] == 100
