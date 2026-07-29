"""Regression: an uncalibrated win probability must be quoted as a baseline.

Incident: a Vendor Active digest listed five unrelated tenders (ICT, facilities,
logistics, consultancy, construction) all at exactly "30.2%". The computation is
correct given its inputs — every one of those tenders was auto-created from the
GeBIZ feed with the placeholder ``base_rate = 0.20``, and the vendor's profile
multipliers are per-vendor, not per-tender, so an unchanged profile produces the
same product every time.

The defect is presentation, not arithmetic: a repeated constant rendered to one
decimal place reads as a tender-specific estimate. ``TenderShortlist`` now
carries ``base_rate_calibrated``; probabilities derived from the placeholder are
rendered "~30%*" with a footnote instead of "30.2%".

Assertions run against the reportlab flowables rather than
``pypdf.extract_text()`` — the matches table paginates, and extracted text
silently drops cells that land across a page break.
"""
import uuid
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services.tender_service import compute_tender_win_probability
from app.workers.tasks import resolve_tender_base_rate


# --------------------------------------------------------------------------
# Layer 1 — the service flags the placeholder
# --------------------------------------------------------------------------
def _db_with_tender(calibrated: bool, base_rate: float = 0.20):
    """Mock Session answering TenderShortlist with a tender of known calibration."""
    tender = MagicMock()
    tender.tender_no = "ITQ202500001"
    tender.sector = "ICT"
    tender.agency = "GovTech"
    tender.description = "Mock tender"
    tender.base_rate = base_rate
    tender.base_rate_calibrated = calibrated

    snap = MagicMock()
    snap.verification_depth = "STANDARD"
    snap.risk_adjusted_pct = 55.0
    snap.evidence_count = 3
    snap.risk_signal = "CLEAN"
    snap.updated_at = datetime.now(timezone.utc)

    db = MagicMock()

    def _side_effect(model):
        m = str(model)
        result = (tender if "TenderShortlist" in m
                  else snap if "VendorStatusSnapshot" in m else None)
        q = MagicMock()
        q.filter.return_value.first.return_value = result
        q.filter.return_value.count.return_value = 0
        return q

    db.query.side_effect = _side_effect
    return db


def test_placeholder_base_rate_is_reported_as_baseline_estimate():
    res = compute_tender_win_probability(
        _db_with_tender(calibrated=False), "ITQ202500001", vendor_id="v1")
    assert res["baselineEstimate"] is True


def test_calibrated_base_rate_is_not_a_baseline_estimate():
    res = compute_tender_win_probability(
        _db_with_tender(calibrated=True, base_rate=0.31), "ITQ202500001", vendor_id="v1")
    assert res["baselineEstimate"] is False


def test_flag_does_not_change_the_computed_probability():
    """The caveat is a label. Silently moving the number would be a worse bug."""
    a = compute_tender_win_probability(
        _db_with_tender(calibrated=False), "ITQ202500001", vendor_id="v1")
    b = compute_tender_win_probability(
        _db_with_tender(calibrated=True), "ITQ202500001", vendor_id="v1")
    assert a["currentProbability"] == b["currentProbability"]


# --------------------------------------------------------------------------
# Layer 2 — the snapshot PDF renders the caveat
# --------------------------------------------------------------------------
def _capture_paragraphs(monkeypatch, module):
    from reportlab.platypus import Paragraph as _RealParagraph

    seen = []

    class _Recording(_RealParagraph):
        def __init__(self, text, *a, **kw):
            seen.append(text)
            super().__init__(text, *a, **kw)

    monkeypatch.setattr(module, "Paragraph", _Recording)
    return seen


def _snapshot_texts(monkeypatch, matches):
    from app.services import vendor_snapshot_generator as gen

    texts = _capture_paragraphs(monkeypatch, gen)
    pdf = gen.generate_vendor_snapshot_pdf({
        "company_name": "Baseline Pte Ltd",
        "plan_label": "Vendor Active",
        "trust_score": 55,
        "compliance_score": 58,
        "tender_matches": matches,
    })
    assert pdf.startswith(b"%PDF")
    return texts


def _match(title, wp, baseline):
    return {
        "title": title,
        "closing_date": date(2026, 9, 1),
        "bid_label": "BID",
        "win_probability": wp,
        "win_probability_is_baseline": baseline,
    }


FOOTNOTE_MARKER = "Baseline estimate"


def test_snapshot_marks_uncalibrated_matches_and_explains_why(monkeypatch):
    """The exact shape from the bug report: five tenders, one placeholder rate."""
    matches = [_match(f"Tender {i}", 30.2, True) for i in range(5)]
    texts = _snapshot_texts(monkeypatch, matches)

    # Every cell carries the tilde, and none quotes a decimal place the input
    # cannot support.
    cells = [t for t in texts if "%" in t and "30" in t]
    assert cells, "no win-probability cells rendered"
    assert all(t == "~30%*" for t in cells), cells
    assert not any("30.2" in t for t in texts)

    assert any(FOOTNOTE_MARKER in t for t in texts), "caveat footnote missing"


def test_snapshot_leaves_calibrated_matches_uncaveated(monkeypatch):
    """A tender with real award history must not be hedged — that dilutes the signal."""
    texts = _snapshot_texts(monkeypatch, [_match("Calibrated tender", 42.0, False)])

    assert any(t == "42%" for t in texts), [t for t in texts if "%" in t]
    assert not any("~" in t and "%" in t for t in texts)
    assert not any(FOOTNOTE_MARKER in t for t in texts)


def test_snapshot_footnote_appears_when_any_match_is_baseline(monkeypatch):
    """Mixed table: the footnote is table-scoped, so one uncalibrated row shows it."""
    texts = _snapshot_texts(monkeypatch, [
        _match("Calibrated", 42.0, False),
        _match("Placeholder", 30.2, True),
    ])

    assert any(t == "42%" for t in texts)
    assert any(t == "~30%*" for t in texts)
    assert any(FOOTNOTE_MARKER in t for t in texts)


def test_snapshot_handles_missing_probability(monkeypatch):
    """A None probability must render an em dash, never "~None%"."""
    texts = _snapshot_texts(monkeypatch, [
        _match("Scored", 42.0, False),
        _match("Unscored", None, True),
    ])

    assert any(t == "—" for t in texts)
    assert not any("None" in t for t in texts)


# --------------------------------------------------------------------------
# Layer 2b — the Vendor Pro monthly report renders the same caveat
#
# Confirmed on a second product after the snapshot fix shipped: eight tenders
# across six unrelated agencies (Singapore Polytechnic, NLB, HSA, STB, MOE x2)
# all quoted "30.2%". Same placeholder base rate, a separate template that
# received `win_probability_is_baseline` from `get_tender_matches` and dropped
# it at render. Both generators must stay in step.
# --------------------------------------------------------------------------
def _pro_report_texts(monkeypatch, matches):
    from app.services import vendor_pro_report_generator as gen

    texts = _capture_paragraphs(monkeypatch, gen)
    pdf = gen.generate_vendor_pro_report_pdf({
        "company_name": "Baseline Pte Ltd",
        "plan_label": "Vendor Pro",
        "tender_matches": matches,
    })
    assert pdf.startswith(b"%PDF")
    return texts


def test_pro_report_marks_uncalibrated_matches_and_explains_why(monkeypatch):
    """The reported shape: eight tenders, one placeholder rate, six agencies."""
    matches = [_match(f"Tender {i}", 30.2, True) for i in range(8)]
    texts = _pro_report_texts(monkeypatch, matches)

    cells = [t for t in texts if "%" in t and "30" in t]
    assert cells, "no win-probability cells rendered"
    assert all(t == "~30%*" for t in cells), cells
    assert not any("30.2" in t for t in texts)

    assert any(FOOTNOTE_MARKER in t for t in texts), "caveat footnote missing"


def test_pro_report_leaves_calibrated_matches_uncaveated(monkeypatch):
    texts = _pro_report_texts(monkeypatch, [_match("Calibrated tender", 42.0, False)])

    assert any(t == "42%" for t in texts), [t for t in texts if "%" in t]
    assert not any("~" in t and "%" in t for t in texts)
    assert not any(FOOTNOTE_MARKER in t for t in texts)


def test_pro_report_footnote_appears_when_any_match_is_baseline(monkeypatch):
    texts = _pro_report_texts(monkeypatch, [
        _match("Calibrated", 42.0, False),
        _match("Placeholder", 30.2, True),
    ])

    assert any(t == "42%" for t in texts)
    assert any(t == "~30%*" for t in texts)
    assert any(FOOTNOTE_MARKER in t for t in texts)


def test_pro_report_handles_missing_probability(monkeypatch):
    """A None probability must render an em dash, never "~None%"."""
    texts = _pro_report_texts(monkeypatch, [
        _match("Scored", 42.0, False),
        _match("Unscored", None, True),
    ])

    assert any(t == "—" for t in texts)
    assert not any("None" in t for t in texts)


# --------------------------------------------------------------------------
# Layer 3 — the auto-create paths write the flag
# --------------------------------------------------------------------------
def test_auto_created_tender_is_marked_uncalibrated(test_db):
    """The GeBIZ stub path is where every placeholder rate in production came from.

    If this default ever flips back to calibrated, the caveat silently stops
    rendering and the incident returns with no test failing.
    """
    from app.core.models import GebizTender

    # Unique per run: the test_db fixture shares one database across the suite
    # and this path commits, so a fixed tender_no collides on the second run.
    tender_no = f"ITQ-AUTOCREATE-{uuid.uuid4().hex[:8]}"
    test_db.add(GebizTender(
        tender_no=tender_no,
        title="Supply of widgets",
        agency="GovTech",
        raw_data={"category": "ICT"},
    ))
    test_db.commit()

    res = compute_tender_win_probability(test_db, tender_no, vendor_id=None)
    assert res.get("error") != "tender_not_found"
    assert res["baselineEstimate"] is True


def test_model_default_is_calibrated(test_db):
    """Admin-imported tenders supply a real base_rate and must not be caveated."""
    from app.core.models import TenderShortlist

    t = TenderShortlist(
        tender_no=f"ITQ-ADMIN-{uuid.uuid4().hex[:8]}",
        description="Hand-imported tender",
        agency="MOH",
        sector="Healthcare",
        base_rate=0.31,
    )
    test_db.add(t)
    test_db.commit()
    test_db.refresh(t)
    assert t.base_rate_calibrated is True


# --------------------------------------------------------------------------
# Layer 4 — calibration clears the flag again (it must not be one-way)
# --------------------------------------------------------------------------
# `refresh_gebiz_base_rates` used to rewrite `base_rate` from real GeBIZ award
# data without ever touching `base_rate_calibrated`. Combined with the two
# auto-create paths that write False, the flag could only ever travel one way:
# every newly-synced tender entered uncalibrated and stayed uncalibrated even
# after it was calibrated. The customer-visible symptom is the footnote
# asserting "limited award history for this category" about a tender calibrated
# from real award records — and, as the caveat spreads to every row, an asterisk
# that carries no information at all.
def test_agency_rate_marks_tender_calibrated():
    rate, calibrated = resolve_tender_base_rate(
        "GovTech", "ICT", {"GOVTECH": 0.42}, {"ICT": 0.31}, 0.20
    )
    assert (rate, calibrated) == (0.42, True)


def test_sector_rate_marks_tender_calibrated():
    """No agency match — the sector rate is still a real calibration."""
    rate, calibrated = resolve_tender_base_rate(
        "MOH", "ICT", {"GOVTECH": 0.42}, {"ICT": 0.31}, 0.20
    )
    assert (rate, calibrated) == (0.31, True)


def test_default_fallback_is_not_a_calibration():
    """The placeholder must never be reported as calibrated."""
    rate, calibrated = resolve_tender_base_rate("MOH", "Healthcare", {}, {}, 0.20)
    assert (rate, calibrated) == (0.20, False)


def test_calibration_flag_is_not_one_way():
    """A previously-uncalibrated tender flips to True once a rate is available.

    This is the regression the extraction exists to guard: assert on the
    provenance decision itself, so removing the flag write in the refresh loop
    fails here rather than surfacing months later in a footnote.
    """
    sector_rates = {"ICT": 0.31}
    _, before = resolve_tender_base_rate("GovTech", "ICT", {}, {}, 0.20)
    _, after = resolve_tender_base_rate("GovTech", "ICT", {}, sector_rates, 0.20)
    assert before is False
    assert after is True


def test_losing_award_data_reverts_flag_to_uncalibrated():
    """Honesty runs both directions: no data means no calibration claim."""
    _, calibrated = resolve_tender_base_rate("GovTech", "ICT", {}, {}, 0.20)
    assert calibrated is False


def test_falsy_rate_is_still_a_calibration():
    """A computed rate at the clamp floor is truthy-adjacent but must not be
    mistaken for "no rate" — the original `or`-chain would have skipped 0.0."""
    rate, calibrated = resolve_tender_base_rate("X", "Y", {"X": 0.0}, {}, 0.20)
    assert (rate, calibrated) == (0.0, True)


# --------------------------------------------------------------------------
# Clamp saturation is not a calibration
# --------------------------------------------------------------------------
# Live evidence: 515 of 523 production tenders sat at exactly base_rate 0.60 —
# CLAMP_MAX — and every one reported base_rate_calibrated=True, so the Vendor Pro
# report rendered ten different tenders at an identical bare "56%" with no "~"
# and no footnote. The underlying ratio is `unique vendors awarded / total
# tenders`, which measures whether a tender was awarded to anyone at all, not
# whether this bidder wins; it runs near 1.0 and pins at the ceiling. When the
# clamp decides the number, the award data did not.

def test_rate_at_clamp_ceiling_is_not_a_calibration():
    rate, calibrated = resolve_tender_base_rate(
        "GovTech", "ICT", {"GOVTECH": 0.60}, {}, 0.20, clamp_max=0.60
    )
    assert rate == 0.60, "the rate itself is still used; only the claim changes"
    assert calibrated is False


def test_rate_below_the_ceiling_remains_a_calibration():
    """The fix must not blanket-suppress the flag — a genuinely calibrated rate
    still gets quoted without the baseline caveat."""
    _, calibrated = resolve_tender_base_rate(
        "GovTech", "ICT", {"GOVTECH": 0.42}, {}, 0.20, clamp_max=0.60
    )
    assert calibrated is True


def test_saturation_rule_applies_to_sector_rates_too():
    """Agency and sector rates come off the same saturating ratio, so the sector
    fallback cannot be the loophole that keeps claiming calibration."""
    _, calibrated = resolve_tender_base_rate(
        "MOH", "ICT", {}, {"ICT": 0.60}, 0.20, clamp_max=0.60
    )
    assert calibrated is False


def test_omitting_clamp_max_preserves_legacy_behaviour():
    """`clamp_max` is optional; callers that don't know the ceiling (tests, the
    auto-create path) must not silently start reporting False for real rates."""
    _, calibrated = resolve_tender_base_rate("GovTech", "ICT", {"GOVTECH": 0.60}, {}, 0.20)
    assert calibrated is True


def test_a_saturated_tender_renders_the_baseline_caveat_end_to_end():
    """The point of the flag: the service layer must now emit the caveat for a
    tender whose rate was clamp-decided, which is what puts "~N%*" and the
    footnote back into the Vendor Pro report."""
    _, calibrated = resolve_tender_base_rate(
        "GovTech", "ICT", {"GOVTECH": 0.60}, {}, 0.20, clamp_max=0.60
    )
    db = _db_with_tender(calibrated, base_rate=0.60)
    res = compute_tender_win_probability(db, "TENDER-1", None)
    assert res["baselineEstimate"] is True
