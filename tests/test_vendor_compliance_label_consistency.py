"""Regression: one account must never show two different "Compliance" numbers.

Incident: the Vendor Status Snapshot rendered a headline card
"COMPLIANCE SCORE / 100: 58" (``pdpa_findings.latest_pdpa_score``) directly
above a Trust Score Breakdown row "Compliance: 76/100"
(``VendorScore.compliance_score``, a verification + notarized-proof composite
from ``scoring.calculate_compliance_score``). Two structurally different
metrics shared one label in one PDF, 18 points apart.

The fix is Option A: the composite keeps its column and formula but is labelled
"Verification & Documentation" everywhere it renders. The invariant these tests
pin is Gianpaolo's acceptance criterion:

    every figure labelled "Compliance" in any customer-facing document for an
    account resolves to the same number — the canonical PDPA score.

Assertions run against the reportlab flowables rather than
``pypdf.extract_text()``: the breakdown table paginates, and extracted text
silently drops cells that land across a page break, which would let this
regression pass unnoticed.
"""
from datetime import datetime, timedelta, timezone

import pytest

PDPA_SCORE = 58        # canonical — what "Compliance" must always mean
COMPOSITE_SCORE = 76   # VendorScore.compliance_score — must never be "Compliance"


# --------------------------------------------------------------------------
# Flowable capture
# --------------------------------------------------------------------------
def _capture_paragraphs(monkeypatch, module):
    """Record the text of every Paragraph the generator builds, in story order.

    Card values/labels, breakdown cells and the trend line are all Paragraphs,
    so one hook sees every rendered string. Order is preserved, which is what
    lets us tie a label to the figure beside it.
    """
    from reportlab.platypus import Paragraph as _RealParagraph

    seen = []

    class _Recording(_RealParagraph):
        def __init__(self, text, *a, **kw):
            seen.append(text)
            super().__init__(text, *a, **kw)

    monkeypatch.setattr(module, "Paragraph", _Recording)
    return seen


def _compliance_labelled(texts):
    """Indices of strings that present themselves as a "Compliance" figure.

    Deliberately substring-based and case-insensitive: the bug shipped as
    "Compliance" in one place and "COMPLIANCE SCORE / 100" in another, and a
    future regression could reintroduce either.
    """
    return [i for i, t in enumerate(texts) if "compliance" in t.lower()]


def _neighbourhood(texts, i):
    """A label and the figure rendered immediately before/after it.

    Card = value then label; table row = label then score. Checking both sides
    covers the two layouts without hard-coding either.
    """
    return [texts[j] for j in (i - 1, i, i + 1) if 0 <= j < len(texts)]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _vendor_with_divergent_scores(db, email="vendor+labelclash@booppa.io"):
    """A vendor whose PDPA score (58) and activity composite (76) disagree.

    This is the real production shape: notarized proofs push
    ``calculate_compliance_score`` far above the PDPA scan, because the PDPA
    score contributes at most 17 of the composite's 100 points.
    """
    from app.core.models import Report, User, VendorScore

    user = User(email=email, hashed_password="x", role="VENDOR",
                plan="vendor_active", company="Clash Pte Ltd")
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Clash Pte Ltd",
        assessment_data={"compliance_score": PDPA_SCORE},
        status="completed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    db.add(VendorScore(
        vendor_id=user.id,
        compliance_score=COMPOSITE_SCORE,
        visibility_score=50,
        engagement_score=40,
        procurement_interest_score=30,
        total_score=55,
    ))
    db.commit()
    return user


# --------------------------------------------------------------------------
# Source-of-truth: the two numbers really are different for this vendor
# --------------------------------------------------------------------------
def test_pdpa_score_and_composite_diverge_for_this_vendor(test_db):
    """Guards the fixture itself — if these ever converge the test proves nothing."""
    from app.services.pdpa_findings import latest_pdpa_score

    user = _vendor_with_divergent_scores(test_db)
    assert latest_pdpa_score(test_db, user.id) == PDPA_SCORE
    assert PDPA_SCORE != COMPOSITE_SCORE


def test_breakdown_never_labels_the_composite_compliance(test_db):
    from app.services.vendor_active_insights import get_trust_breakdown

    user = _vendor_with_divergent_scores(test_db, "vendor+bdlabel@booppa.io")
    bd = get_trust_breakdown(test_db, str(user.id))
    dims = {d["label"]: d for d in bd["dimensions"]}

    assert "Compliance" not in dims
    assert dims["Verification & Documentation"]["score"] == COMPOSITE_SCORE
    # The improvement action must name the lever that actually moves this row.
    # It previously told vendors to run a PDPA scan — worth at most 17 points
    # of a score dominated by notarization.
    action = dims["Verification & Documentation"]["action"].lower()
    assert "notariz" in action or "verification level" in action


# --------------------------------------------------------------------------
# Snapshot PDF — the document in the bug report
# --------------------------------------------------------------------------
def test_snapshot_shows_one_compliance_number(test_db, monkeypatch):
    from app.services import vendor_snapshot_generator as gen
    from app.services.pdpa_findings import latest_pdpa_score
    from app.services.vendor_active_insights import get_trust_breakdown

    user = _vendor_with_divergent_scores(test_db, "vendor+snap@booppa.io")
    texts = _capture_paragraphs(monkeypatch, gen)

    pdf = gen.generate_vendor_snapshot_pdf({
        "company_name": "Clash Pte Ltd",
        "plan_label": "Vendor Active",
        "trust_score": 55,
        "compliance_score": latest_pdpa_score(test_db, user.id),
        "profile_views_30d": 12,
        "trust_breakdown": get_trust_breakdown(test_db, str(user.id)),
        "trend": {"total_delta": 3, "compliance_delta": 5},
    })
    assert pdf.startswith(b"%PDF")

    labelled = _compliance_labelled(texts)
    assert labelled, "no Compliance figure rendered at all"

    # THE invariant: no figure presented as "Compliance" may be the composite.
    for i in labelled:
        around = _neighbourhood(texts, i)
        assert not any(str(COMPOSITE_SCORE) in t for t in around), (
            f"composite {COMPOSITE_SCORE} rendered under a Compliance label: {around}"
        )

    # ...and the headline card carries the canonical PDPA score.
    card = next(i for i in labelled if "COMPLIANCE SCORE / 100" in texts[i])
    assert texts[card - 1] == str(PDPA_SCORE)

    # The composite is still shown — under its own honest label, not dropped.
    # Table cells are _xml_escape'd, so the ampersand arrives as "&amp;".
    vd = next(i for i, t in enumerate(texts)
              if t in ("Verification & Documentation", "Verification &amp; Documentation"))
    assert texts[vd + 1] == f"{COMPOSITE_SCORE}/100"


def test_snapshot_states_absence_rather_than_borrowing_the_composite(monkeypatch):
    """No PDPA scan must read "not assessed", never a substituted number.

    A vendor with notarized proofs but no scan would otherwise show a healthy
    "COMPLIANCE SCORE / 100: 76" sourced entirely from platform activity — a
    compliance claim with no compliance evidence behind it.
    """
    from app.services import vendor_snapshot_generator as gen

    texts = _capture_paragraphs(monkeypatch, gen)
    gen.generate_vendor_snapshot_pdf({
        "company_name": "Unscanned Pte Ltd",
        "plan_label": "Vendor Active",
        "trust_score": 55,
        "compliance_score": None,
        "trust_breakdown": {
            "total": 55,
            "projected_total": 90,
            "dimensions": [{
                "label": "Verification & Documentation",
                "score": COMPOSITE_SCORE,
                "action": "Notarize a proof document",
                "potential_points": 7,
            }],
            "top_actions": [],
        },
    })

    assert any("NO PDPA SCAN" in t for t in texts)
    for i in _compliance_labelled(texts):
        assert not any(str(COMPOSITE_SCORE) in t for t in _neighbourhood(texts, i))


# --------------------------------------------------------------------------
# Vendor Pro monthly report — same collision, and it named PDPA explicitly
# --------------------------------------------------------------------------
def test_pro_report_shows_one_compliance_number(test_db, monkeypatch):
    """The Pro report contradicted itself inside a single document.

    Its headline card showed the composite while the drift section below it
    read "Current PDPA compliance score: 58/100" — the same two numbers, with
    the second one explicitly naming PDPA.
    """
    from app.services import vendor_pro_report_generator as gen

    user = _vendor_with_divergent_scores(test_db, "vendor+pro@booppa.io")
    texts = _capture_paragraphs(monkeypatch, gen)

    pdf = gen.build_pro_report_pdf(test_db, str(user.id))
    assert pdf is None or pdf.startswith(b"%PDF")

    for i in _compliance_labelled(texts):
        around = _neighbourhood(texts, i)
        assert not any(str(COMPOSITE_SCORE) in t for t in around), (
            f"composite {COMPOSITE_SCORE} rendered under a Compliance label: {around}"
        )
