"""
Tests for tender_service.compute_tender_win_probability.

Uses an in-memory SQLite database to avoid requiring a real PostgreSQL instance.
The service is purely read-only (no writes), so a minimal fixture is enough.
"""

import uuid
import pytest
from unittest.mock import MagicMock, patch

from app.services.tender_service import (
    compute_tender_win_probability,
    _compute_raw_probability,
    _build_gap_reasons,
    _sector_mult,
    _evidence_mult,
    max_depth,
    PROFILE_MULT,
    RISK_PENALTY,
    MAX_PROBABILITY,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_tender(tender_no="ITQ202500001", sector="ICT", agency="GovTech", base_rate=0.25):
    t = MagicMock()
    t.tender_no    = tender_no
    t.sector       = sector
    t.agency       = agency
    t.description  = "Mock tender description"
    t.base_rate    = base_rate
    return t


def _mock_snapshot(
    verification_depth="STANDARD",
    risk_adjusted_pct=55.0,
    evidence_count=3,
    risk_signal="CLEAN",
):
    s = MagicMock()
    s.verification_depth = verification_depth
    s.risk_adjusted_pct  = risk_adjusted_pct
    s.evidence_count     = evidence_count
    s.risk_signal        = risk_signal
    return s


def _make_db(tender=None, snapshot=None):
    """Return a mock Session where query().filter().first() returns preset objects."""
    db = MagicMock()

    def _query_side_effect(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = (
            tender if "TenderShortlist" in str(model) else snapshot
        )
        return q

    db.query.side_effect = _query_side_effect
    return db


# ── Unit tests: formula helpers ───────────────────────────────────────────────

class TestSectorMult:
    def test_top_quartile(self):
        assert _sector_mult(80) == 1.15

    def test_median(self):
        assert _sector_mult(50) == 1.00

    def test_lower_quartile(self):
        assert _sector_mult(30) == 0.90

    def test_bottom_quartile(self):
        assert _sector_mult(10) == 0.80


class TestEvidenceMult:
    def test_zero(self):
        assert _evidence_mult(0) == 0.80

    def test_one(self):
        assert _evidence_mult(1) == 0.95

    def test_three(self):
        assert _evidence_mult(3) == 1.05

    def test_six(self):
        assert _evidence_mult(6) == 1.15


class TestMaxDepth:
    def test_certified_beats_deep(self):
        assert max_depth("DEEP", "CERTIFIED") == "CERTIFIED"

    def test_deep_beats_standard(self):
        assert max_depth("STANDARD", "DEEP") == "DEEP"

    def test_same_depth(self):
        assert max_depth("STANDARD", "STANDARD") == "STANDARD"

    def test_unverified_upgraded(self):
        assert max_depth("UNVERIFIED", "BASIC") == "BASIC"


class TestRawProbability:
    def test_probability_capped_at_max(self):
        # A perfect profile with a high base_rate should be capped
        p = _compute_raw_probability(0.99, "CERTIFIED", 99, 10, "CLEAN")
        assert p == MAX_PROBABILITY

    def test_critical_risk_slashes_probability(self):
        p_clean    = _compute_raw_probability(0.25, "DEEP", 60, 4, "CLEAN")
        p_critical = _compute_raw_probability(0.25, "DEEP", 60, 4, "CRITICAL")
        assert p_critical < p_clean * 0.5

    def test_unverified_is_lower_than_certified(self):
        p_unverified = _compute_raw_probability(0.25, "UNVERIFIED", 50, 3, "CLEAN")
        p_certified  = _compute_raw_probability(0.25, "CERTIFIED",  50, 3, "CLEAN")
        assert p_certified > p_unverified

    def test_result_between_zero_and_one(self):
        for depth in PROFILE_MULT:
            for risk in RISK_PENALTY:
                p = _compute_raw_probability(0.20, depth, 50, 3, risk)
                assert 0.0 <= p <= 1.0, f"Out of range for depth={depth}, risk={risk}"


class TestGapReasons:
    def test_unverified_generates_reason(self):
        reasons = _build_gap_reasons("UNVERIFIED", 50, 3, "CLEAN")
        assert any("UNVERIFIED" in r for r in reasons)

    def test_below_median_generates_reason(self):
        reasons = _build_gap_reasons("DEEP", 20, 5, "CLEAN")
        assert any("20th" in r for r in reasons)

    def test_low_evidence_generates_reason(self):
        reasons = _build_gap_reasons("STANDARD", 55, 0, "CLEAN")
        assert any("0 verified evidence" in r for r in reasons)

    def test_flagged_risk_generates_reason(self):
        reasons = _build_gap_reasons("CERTIFIED", 80, 6, "FLAGGED")
        assert any("FLAGGED" in r for r in reasons)

    def test_strong_profile_returns_positive_reason(self):
        reasons = _build_gap_reasons("CERTIFIED", 80, 6, "CLEAN")
        assert len(reasons) == 1
        assert "competitive" in reasons[0].lower()


# ── Integration-style tests: compute_tender_win_probability ───────────────────

class TestComputeTenderWinProbability:
    def test_tender_not_found_returns_error(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = compute_tender_win_probability(db, "FAKE-TENDER")
        assert result["error"] == "tender_not_found"

    def test_guest_view_has_no_projections(self):
        db = _make_db(tender=_mock_tender(), snapshot=None)
        result = compute_tender_win_probability(db, "ITQ202500001", vendor_id=None)

        assert result["tenderNo"] == "ITQ202500001"
        assert result["projections"] is None
        assert result["vendorProfile"] is None
        assert result["gapReasons"] == []

    def test_unverified_vendor_has_low_probability(self):
        snap = _mock_snapshot(verification_depth="UNVERIFIED", risk_adjusted_pct=30, evidence_count=0)
        db   = _make_db(tender=_mock_tender(base_rate=0.25), snapshot=snap)
        vid  = str(uuid.uuid4())

        result = compute_tender_win_probability(db, "ITQ202500001", vendor_id=vid)
        assert result["currentProbability"] < 15  # unverified with low rank should be very low

    def test_certified_vendor_has_higher_probability_than_unverified(self):
        tender = _mock_tender(base_rate=0.25)

        snap_unverified = _mock_snapshot("UNVERIFIED", 50, 0, "CLEAN")
        db_unverified   = _make_db(tender=tender, snapshot=snap_unverified)
        p_unverified    = compute_tender_win_probability(
            db_unverified, "ITQ202500001", str(uuid.uuid4())
        )["currentProbability"]

        snap_certified = _mock_snapshot("CERTIFIED", 80, 6, "CLEAN")
        db_certified   = _make_db(tender=tender, snapshot=snap_certified)
        p_certified    = compute_tender_win_probability(
            db_certified, "ITQ202500001", str(uuid.uuid4())
        )["currentProbability"]

        assert p_certified > p_unverified

    def test_express_probability_geq_current(self):
        snap   = _mock_snapshot("BASIC", 40, 1, "CLEAN")
        db     = _make_db(tender=_mock_tender(), snapshot=snap)
        result = compute_tender_win_probability(db, "ITQ202500001", str(uuid.uuid4()))

        assert result["projections"]["rfpExpress"]["probability"] >= result["currentProbability"]

    def test_complete_probability_geq_express(self):
        snap   = _mock_snapshot("STANDARD", 50, 2, "CLEAN")
        db     = _make_db(tender=_mock_tender(), snapshot=snap)
        result = compute_tender_win_probability(db, "ITQ202500001", str(uuid.uuid4()))

        p_express  = result["projections"]["rfpExpress"]["probability"]
        p_complete = result["projections"]["rfpComplete"]["probability"]
        assert p_complete >= p_express

    def test_result_contains_expected_keys(self):
        snap   = _mock_snapshot()
        db     = _make_db(tender=_mock_tender(), snapshot=snap)
        result = compute_tender_win_probability(db, "ITQ202500001", str(uuid.uuid4()))

        for key in ("tenderNo", "sector", "agency", "currentProbability",
                    "vendorProfile", "projections", "gapReasons"):
            assert key in result, f"Missing key: {key}"

    def test_deep_vendor_matches_expected_math(self):
        """Verify the arithmetic precisely for a known input set."""
        # base_rate=0.20, DEEP=1.10, sector_pct=60 → s_mult=1.00,
        # evidence=4 → e_mult=1.05, CLEAN → r_pen=1.00
        # expected = 0.20 * 1.10 * 1.00 * 1.05 * 1.00 = 0.231 → 23.1%
        snap   = _mock_snapshot("DEEP", 60, 4, "CLEAN")
        db     = _make_db(tender=_mock_tender(base_rate=0.20), snapshot=snap)
        result = compute_tender_win_probability(db, "ITQ202500001", str(uuid.uuid4()))

        assert abs(result["currentProbability"] - 23.1) < 0.5
