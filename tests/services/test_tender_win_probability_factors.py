"""Regression tests for the per-tender win-probability factors.

Locks the fix for the "constant 33.6% on every tender" defect: two tenders
scored against the *same* vendor snapshot must produce different probabilities
when they differ in contract value and/or closing deadline.
"""

from datetime import datetime, timezone, timedelta

from app.services.tender_service import (
    _compute_raw_probability,
    _value_fit_mult,
    _deadline_comfort_mult,
)


def test_value_fit_varies_with_absolute_size_when_range_unknown():
    # No demonstrated vendor range → size-based curve, still non-constant.
    small = _value_fit_mult(50_000, None)
    mid = _value_fit_mult(500_000, None)
    large = _value_fit_mult(10_000_000, None)
    assert small > mid > large
    assert small != large


def test_value_fit_rewards_closeness_to_vendor_range():
    typical = 1_000_000.0
    near = _value_fit_mult(1_200_000, typical)      # within ~2x
    far = _value_fit_mult(50_000_000, typical)       # >order of magnitude off
    assert near > far


def test_value_fit_neutral_without_tender_value():
    assert _value_fit_mult(None, None) == 1.0
    assert _value_fit_mult(0, 1_000_000) == 1.0


def test_deadline_comfort_penalises_tight_windows():
    now = datetime.now(timezone.utc)
    closed = _deadline_comfort_mult(now - timedelta(days=1))
    tight = _deadline_comfort_mult(now + timedelta(days=3))
    workable = _deadline_comfort_mult(now + timedelta(days=14))
    comfortable = _deadline_comfort_mult(now + timedelta(days=45))
    assert closed < tight < workable <= comfortable
    assert comfortable > closed


def test_deadline_neutral_when_unknown():
    assert _deadline_comfort_mult(None) == 1.0


def test_same_vendor_different_tenders_yield_distinct_probabilities():
    # Identical vendor profile; only per-tender value + deadline differ.
    vendor = dict(
        verification_depth="BASIC",
        sector_percentile=50.0,
        evidence_count=1,
        risk_signal="CLEAN",
    )
    now = datetime.now(timezone.utc)

    tender_a = _compute_raw_probability(
        0.20, **vendor,
        value_fit_mult=_value_fit_mult(80_000, None),
        deadline_mult=_deadline_comfort_mult(now + timedelta(days=40)),
    )
    tender_b = _compute_raw_probability(
        0.20, **vendor,
        value_fit_mult=_value_fit_mult(20_000_000, None),
        deadline_mult=_deadline_comfort_mult(now + timedelta(days=2)),
    )
    assert tender_a != tender_b
    assert tender_a > tender_b  # small/comfortable beats huge/tight


def test_factors_default_to_neutral_and_preserve_legacy_score():
    # Omitting the new args reproduces the pre-change score exactly.
    legacy = _compute_raw_probability(0.20, "BASIC", 50.0, 1, "CLEAN")
    explicit = _compute_raw_probability(
        0.20, "BASIC", 50.0, 1, "CLEAN", value_fit_mult=1.0, deadline_mult=1.0
    )
    assert legacy == explicit
