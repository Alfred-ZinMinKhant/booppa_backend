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
        assert "PDPC has fined" in s
        assert "S$" in s

    def test_unknown_key_returns_none(self):
        assert precedent_summary("unknown:bogus") is None

    def test_summary_includes_vendor_name(self):
        s = precedent_summary("breach:pdpc_enforcement")
        assert s is not None
        # SingHealth is the seeded case for this key
        assert "SingHealth" in s

    def test_summary_uses_singular_for_one_case(self):
        # NRIC seed has exactly one case
        s = precedent_summary("nric:collection")
        assert s is not None
        assert "1 organisation" in s
        assert "organisations" not in s.replace("1 organisation", "")

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
    def test_million_threshold_rendered_with_decimal(self):
        # SingHealth seed: S$1,000,000 — should render as "S$1.0M"
        s = precedent_summary("breach:pdpc_enforcement")
        assert s is not None
        assert "S$1.0M" in s

    def test_thousands_rendered_with_k_suffix(self):
        # K Box seed: S$50,000 — should render as "S$50k"
        s = precedent_summary("nric:collection")
        assert s is not None
        assert "S$50k" in s
