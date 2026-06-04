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
    classify_clauses_multilingual,
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


CHINESE_POLICY = """
<html><body>
<h1>隐私政策</h1>
<p>我们收集个人数据的目的是为您提供服务、发送营销通讯以及遵守法律义务。</p>
<p>您可以随时通过联系我们的数据保护官 (dpo@example.sg) 撤回您的同意。</p>
<p>我们保留个人数据的时间不会超过实现收集目的所需的时间。</p>
<p>我们可能会向第三方服务提供商披露数据。</p>
<p>您有权访问和更正您的个人数据。</p>
</body></html>
"""

MALAY_POLICY = """
<html><body>
<h1>Dasar Privasi</h1>
<p>Kami mengumpul data peribadi untuk tujuan menyediakan perkhidmatan kami.</p>
<p>Anda boleh menarik balik persetujuan anda pada bila-bila masa.</p>
</body></html>
"""


class TestMultilingual:
    def test_empty_policy_returns_all_uncertain(self):
        verdicts = asyncio.run(classify_clauses_multilingual("", language="zh", provider=None))
        assert len(verdicts) == len(CLAUSES)
        assert all(not v.present for v in verdicts)

    def test_chinese_no_provider_returns_uncertain_with_language_note(self):
        verdicts = asyncio.run(
            classify_clauses_multilingual(CHINESE_POLICY, language="zh", provider=None)
        )
        assert len(verdicts) == len(CLAUSES)
        # Without an LLM we honestly can't classify CN text — all marked uncertain
        for v in verdicts:
            assert v.present is False
            assert "zh" in v.note or "Non-English" in v.note

    def test_malay_no_provider_returns_uncertain(self):
        verdicts = asyncio.run(
            classify_clauses_multilingual(MALAY_POLICY, language="ms", provider=None)
        )
        assert len(verdicts) == len(CLAUSES)
        assert all(not v.present for v in verdicts)

    def test_returns_one_verdict_per_clause(self):
        verdicts = asyncio.run(
            classify_clauses_multilingual(CHINESE_POLICY, language="zh", provider=None)
        )
        clauses_returned = {v.clause for v in verdicts}
        assert clauses_returned == set(CLAUSES)

    def test_html_is_stripped_before_processing(self):
        # Even with raw HTML, the multilingual path should not crash
        verdicts = asyncio.run(
            classify_clauses_multilingual(
                "<html><body><p>政策内容</p></body></html>",
                language="zh", provider=None,
            )
        )
        assert len(verdicts) == len(CLAUSES)
