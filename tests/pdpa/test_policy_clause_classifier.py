"""Tests for app.services.policy_clause_classifier.

Covers snippet harvesting, the heuristic (no-LLM) fallback, the dimension
roll-up, and the specific withdrawal-regex regression we hit during Tier 2
development ('withdraw your consent' must be detected).
"""
import asyncio

import pytest

from app.services.policy_clause_classifier import (
    CLAUSES,
    classify_clauses,
    harvest_clause_snippets,
    summarise,
)


COMPLETE_POLICY = """
<html><body>
<h1>Privacy Policy</h1>
<p>We collect personal data for the purpose of providing our services,
   sending marketing communications, and complying with legal obligations.</p>
<p>You may withdraw your consent at any time by contacting our Data Protection Officer.</p>
<p>Our DPO can be reached at dpo@acme.sg.</p>
<p>We retain personal data for as long as necessary, and will delete it
   when no longer required.</p>
<p>We may disclose data to third-party service providers and overseas processors.</p>
<p>You have the right to access and correction of your personal data.</p>
</body></html>
"""

PARTIAL_POLICY = """
<html><body>
<p>We collect personal data for the purpose of providing services.</p>
<p>Contact us at info@acme.sg.</p>
</body></html>
"""

EMPTY_POLICY = "<html><body><h1>Privacy</h1><p>Coming soon.</p></body></html>"


class TestHarvest:
    def test_finds_all_six_clauses_in_complete_policy(self):
        snippets = harvest_clause_snippets(COMPLETE_POLICY)
        # Every clause should yield at least one snippet
        for clause in CLAUSES:
            assert snippets[clause], f"clause '{clause}' missed in complete policy"

    def test_returns_empty_clauses_for_empty_policy(self):
        snippets = harvest_clause_snippets(EMPTY_POLICY)
        for clause in CLAUSES:
            assert snippets[clause] == []

    def test_strips_html_tags(self):
        snippets = harvest_clause_snippets("<p>NRIC <b>retention period</b> stated</p>")
        # Should at least find retention via the anchor
        assert snippets["retention"]
        # And no raw tags left in the snippet
        assert "<b>" not in snippets["retention"][0]

    def test_withdraw_your_consent_variant_detected(self):
        """Regression: the heuristic anchor must match 'withdraw your consent',
        not just 'withdraw consent' / 'withdrawal of consent'."""
        snippets = harvest_clause_snippets(
            "<p>You may withdraw your consent at any time.</p>"
        )
        assert snippets["withdrawal"], "withdrawal regex regressed"


class TestHeuristicClassify:
    def test_complete_policy_marks_all_clauses_present(self):
        snippets = harvest_clause_snippets(COMPLETE_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        present = [v for v in verdicts if v.present]
        assert len(present) == 6, [v.clause for v in verdicts if not v.present]

    def test_empty_policy_marks_all_clauses_missing(self):
        snippets = harvest_clause_snippets(EMPTY_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        assert all(not v.present for v in verdicts)

    def test_partial_policy_yields_partial_result(self):
        snippets = harvest_clause_snippets(PARTIAL_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        # Should have at least 'purpose' present
        present_clauses = {v.clause for v in verdicts if v.present}
        assert "purpose" in present_clauses
        # And several missing
        assert len(present_clauses) < 6


class TestSummarise:
    def test_complete_policy_compliant(self):
        snippets = harvest_clause_snippets(COMPLETE_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        summary = summarise(verdicts)
        assert summary["status"] == "Compliant"
        assert summary["score"] >= 85
        assert summary["missing"] == []

    def test_empty_policy_non_compliant(self):
        snippets = harvest_clause_snippets(EMPTY_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        summary = summarise(verdicts)
        assert summary["status"] == "Non-Compliant"
        assert summary["score"] == 0
        assert set(summary["missing"]) == set(CLAUSES)

    def test_items_include_each_clause(self):
        snippets = harvest_clause_snippets(COMPLETE_POLICY)
        verdicts = asyncio.run(classify_clauses(snippets, provider=None))
        summary = summarise(verdicts)
        items_clauses = {i["clause"] for i in summary["items"]}
        assert items_clauses == set(CLAUSES)
