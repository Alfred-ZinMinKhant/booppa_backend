"""Regression tests for the recalibrated BID/WATCH/PASS classifier (Defect B.2).

Before the fix, build_vendor_history returned avg_bid_size=None and
agency_win_rate=0.0, so the size-fit and agency factors never contributed and
the composite could not clear the old BID threshold (65) — every tender came
back WATCH. The fix feeds an honest SECTOR-level avg_bid_size (sector median)
and lowers the thresholds (BID>=55, WATCH>=30) so all three labels are
reachable with real sector data and no fabricated per-vendor win rates.
"""

from datetime import datetime, timezone, timedelta

from app.services.tender_service_bid_classifier import classify_tender


def _tender(days_to_close: int, value: float, title: str = "Cloud services", agency: str = "GovTech"):
    return {
        "closing_date": datetime.now(timezone.utc) + timedelta(days=days_to_close),
        "estimated_value": value,
        "title": title,
        "agency": agency,
        "sector": "IT",
        "status": "open",
    }


def test_bid_reachable_with_honest_sector_signals():
    # Comfortable deadline (20pts) + moderate sector fit (18) + size fit (15)
    # + SME language (10) = 63 >= 55 -> BID, with NO agency win rate.
    history = {"sector_win_rate": 0.15, "agency_win_rate": 0.0, "avg_bid_size": 200_000}
    result = classify_tender(
        _tender(30, 200_000, title="SME innovation pilot for cloud"), history
    )
    assert result["label"] == "BID"


def test_watch_for_middling_fit():
    # Adequate deadline (12) + moderate sector (18) = 30 -> WATCH (no size fit,
    # no SME keyword).
    history = {"sector_win_rate": 0.15, "agency_win_rate": 0.0, "avg_bid_size": None}
    result = classify_tender(_tender(14, 5_000_000, title="Civil works contract"), history)
    assert result["label"] == "WATCH"


def test_pass_when_contract_far_too_large():
    # Hard-stop PASS: value > 5x the sector-median avg_bid_size.
    history = {"sector_win_rate": 0.15, "agency_win_rate": 0.0, "avg_bid_size": 100_000}
    result = classify_tender(_tender(30, 10_000_000), history)
    assert result["label"] == "PASS"


def test_size_fit_factor_contributes_only_when_avg_bid_known():
    # Same tender, with vs without avg_bid_size -> different score/label,
    # proving the sector-median signal actually moves the needle.
    t = _tender(30, 200_000, title="Cloud managed services")
    with_size = classify_tender(t, {"sector_win_rate": 0.15, "avg_bid_size": 200_000})
    without_size = classify_tender(t, {"sector_win_rate": 0.15, "avg_bid_size": None})
    assert with_size["label"] == "BID"
    assert without_size["label"] == "WATCH"


def test_reason_copy_is_sector_estimate_not_verified_win_rate():
    # Copy must read as a sector estimate, never a verified per-vendor win rate.
    history = {"sector_win_rate": 0.15, "avg_bid_size": 200_000}
    reason = classify_tender(_tender(30, 200_000), history)["reason"].lower()
    assert "sector award rate" in reason or "sector fit" in reason
