"""Tests for app.services.pdpc_precedents.

Locks in the contracts the rest of the system depends on:
  - get_precedents() returns a list (never None)
  - precedent_summary() is None when no precedents are seeded
  - Summary formatting includes count, total, and example cases
  - Seed data shape is well-formed (every case has the expected keys)
"""
import pytest

from app.services.pdpc_precedents import (
    PRECEDENTS,
    get_precedents,
    precedent_count,
    precedent_keys,
    precedent_summary,
)


class TestLookup:
    def test_known_key_returns_list(self):
        result = get_precedents("breach:pdpc_enforcement")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_unknown_key_returns_empty_list(self):
        assert get_precedents("unknown:bogus") == []

    def test_none_key_safe(self):
        assert get_precedents("") == []

    def test_returns_copy_not_reference(self):
        # Mutating the returned list must not affect the underlying data
        result = get_precedents("breach:pdpc_enforcement")
        original_len = len(PRECEDENTS["breach:pdpc_enforcement"])
        result.append({"vendor": "Mutation Co"})
        assert len(PRECEDENTS["breach:pdpc_enforcement"]) == original_len


class TestSummary:
    def test_known_key_returns_string(self):
        s = precedent_summary("breach:pdpc_enforcement")
        assert s is not None
        assert "PDPC has" in s
        assert "S$" in s

    def test_unknown_key_returns_none(self):
        assert precedent_summary("unknown:bogus") is None

    def test_summary_includes_a_seeded_vendor_name(self):
        # Don't pin to a specific vendor name — the compliance team curates
        # the seed list independently. Assert that whichever vendor names
        # ARE in the seed for this key actually appear in the summary.
        s = precedent_summary("breach:pdpc_enforcement")
        assert s is not None
        seeded_vendors = [
            c["vendor"] for c in PRECEDENTS["breach:pdpc_enforcement"][:2]
            if c.get("vendor")
        ]
        assert seeded_vendors, "precondition: seed must have at least one named vendor"
        assert any(v in s for v in seeded_vendors), (
            f"none of {seeded_vendors} appeared in summary: {s}"
        )

    def test_summary_uses_singular_for_one_case(self, monkeypatch):
        # Inject a synthetic single-entry bucket so this test is decoupled
        # from the real seed list (which gets re-curated periodically).
        monkeypatch.setitem(PRECEDENTS, "test:single", [
            {
                "vendor": "Test Co", "year": 2020, "fine_sgd": 10_000,
                "section": "§24", "url": "https://www.pdpc.gov.sg/test",
                "summary": "synthetic",
            },
        ])
        s = precedent_summary("test:single")
        assert s is not None
        assert "1 organisation" in s
        # Make sure we used "organisation" (singular), not "organisations"
        assert "organisations" not in s

    def test_max_items_limits_cases_shown(self):
        # When max_items=0, no case names should appear
        s = precedent_summary("breach:pdpc_enforcement", max_items=0)
        assert s is not None
        assert "Notable cases" not in s


class TestSeedDataShape:
    REQUIRED_KEYS = {"vendor", "year", "fine_sgd", "section", "url", "summary"}

    def test_every_case_has_required_keys(self):
        for key, cases in PRECEDENTS.items():
            for case in cases:
                missing = self.REQUIRED_KEYS - set(case.keys())
                assert not missing, f"{key}: case missing keys {missing}"

    def test_year_is_int(self):
        for cases in PRECEDENTS.values():
            for case in cases:
                assert isinstance(case["year"], int), f"year not int in {case}"
                assert 2000 <= case["year"] <= 2030

    def test_fine_is_non_negative_int(self):
        for cases in PRECEDENTS.values():
            for case in cases:
                assert isinstance(case["fine_sgd"], int)
                assert case["fine_sgd"] >= 0

    def test_urls_are_pdpc_or_official(self):
        for cases in PRECEDENTS.values():
            for case in cases:
                url = case["url"]
                assert url.startswith("https://"), f"non-https url: {url}"
                # Sanity: must be an official Singapore source
                assert any(d in url for d in ("pdpc.gov.sg", "agc.gov.sg")), f"non-official url: {url}"

    def test_count_helper_matches_real_total(self):
        manual = sum(len(v) for v in PRECEDENTS.values())
        assert precedent_count() == manual

    def test_keys_helper_returns_non_empty_keys_only(self):
        for k in precedent_keys():
            assert PRECEDENTS[k], f"key {k} listed but has no cases"


class TestSummaryFormatting:
    """Formatter tests use monkeypatched buckets so they're decoupled from
    whatever the real seed list happens to total at any given time."""

    def test_million_threshold_rendered_with_decimal(self, monkeypatch):
        monkeypatch.setitem(PRECEDENTS, "test:million", [
            {
                "vendor": "Big Co", "year": 2019, "fine_sgd": 1_000_000,
                "section": "§24", "url": "https://www.pdpc.gov.sg/test",
                "summary": "synthetic",
            },
        ])
        s = precedent_summary("test:million")
        assert s is not None
        assert "S$1.0M" in s

    def test_thousands_rendered_with_k_suffix(self, monkeypatch):
        monkeypatch.setitem(PRECEDENTS, "test:thousands", [
            {
                "vendor": "Mid Co", "year": 2020, "fine_sgd": 50_000,
                "section": "§24", "url": "https://www.pdpc.gov.sg/test",
                "summary": "synthetic",
            },
        ])
        s = precedent_summary("test:thousands")
        assert s is not None
        assert "S$50k" in s

    def test_real_seed_total_uses_k_suffix(self):
        """Sanity-check against the real seed: aggregate is in 'k' range
        and the suffix is correct."""
        total = sum(c["fine_sgd"] for c in PRECEDENTS.get("breach:pdpc_enforcement", []))
        if not (1_000 <= total < 1_000_000):
            pytest.skip(f"seed total S${total} not in k-range; revisit when corpus grows")
        s = precedent_summary("breach:pdpc_enforcement")
        assert s is not None
        assert "k under similar facts" in s
