"""Regression: the Verification & Documentation dimension must carry no PDPA signal.

``VendorScore.compliance_score`` is rendered as "Verification & Documentation"
(see ``vendor_active_insights._TRUST_DIMENSIONS``). It used to add an 8-25 point
bonus derived from the vendor's latest PDPA scan, which produced two problems:

1. The dimension was non-monotonic in what it claims to measure — merely
   *having* a scan earned +8 regardless of the result, so a failing scan raised
   a documentation score.
2. It left the figure partially correlated with the real PDPA score, so the two
   numbers in the same PDF read as one value gone stale rather than as two
   different measurements. That is the confusion behind the 58-vs-76 incident;
   relabelling alone would not have removed it.

The dimension is now a pure function of verification level and notarized proof
count. These tests fail if a PDPA signal is reintroduced.
"""
from datetime import datetime, timedelta, timezone

from app.core.models import (
    LifecycleStatus,
    Proof,
    Report,
    User,
    VerifyRecord,
)
from app.services.scoring import VendorScoreEngine


def _vendor(db, email):
    u = User(email=email, hashed_password="x", role="VENDOR",
             plan="vendor_active", company="Vendor Pte Ltd",
             website="https://vendor.com.sg")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _verify_record(db, vendor, score=50):
    v = VerifyRecord(vendor_id=vendor.id, compliance_score=score,
                     lifecycle_status=LifecycleStatus.ACTIVE)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _pdpa_scan(db, vendor, score):
    db.add(Report(
        owner_id=vendor.id,
        framework="pdpa_quick_scan",
        company_name="Vendor Pte Ltd",
        company_website="https://vendor.com.sg",
        assessment_data={"compliance_score": score},
        status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        completed_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    db.commit()


def test_pdpa_scan_does_not_move_the_verification_dimension(test_db):
    """The decisive assertion: same inputs, scan added, score unchanged."""
    v = _vendor(test_db, "v+nopdpa1@booppa.io")
    _verify_record(test_db, v, score=50)

    before = VendorScoreEngine.calculate_compliance_score(test_db, v.id)
    _pdpa_scan(test_db, v, score=95)
    after = VendorScoreEngine.calculate_compliance_score(test_db, v.id)

    assert before == after


def test_a_failing_scan_cannot_raise_the_score(test_db):
    """The old +8 floor meant a 0-scoring scan still earned points."""
    v = _vendor(test_db, "v+nopdpa2@booppa.io")
    _verify_record(test_db, v, score=50)

    before = VendorScoreEngine.calculate_compliance_score(test_db, v.id)
    _pdpa_scan(test_db, v, score=0)

    assert VendorScoreEngine.calculate_compliance_score(test_db, v.id) == before


def test_score_is_verification_plus_proofs_only(test_db):
    """Pins the formula: level-weighted verification score + 8/proof (first 5)."""
    v = _vendor(test_db, "v+nopdpa3@booppa.io")
    rec = _verify_record(test_db, v, score=50)
    for _ in range(3):
        test_db.add(Proof(verify_id=rec.id))
    test_db.commit()

    # base 50 (BASIC level, weight 1.0) + 3 proofs * 8 = 74
    assert VendorScoreEngine.calculate_compliance_score(test_db, v.id) == 74


def test_notarizing_proof_is_what_moves_the_dimension(test_db):
    """Monotonic in its stated lever — this is what the UI action promises."""
    v = _vendor(test_db, "v+nopdpa4@booppa.io")
    rec = _verify_record(test_db, v, score=40)

    before = VendorScoreEngine.calculate_compliance_score(test_db, v.id)
    test_db.add(Proof(verify_id=rec.id))
    test_db.commit()

    assert VendorScoreEngine.calculate_compliance_score(test_db, v.id) > before


def test_no_verification_record_is_zero(test_db):
    v = _vendor(test_db, "v+nopdpa5@booppa.io")
    _pdpa_scan(test_db, v, score=95)

    # A PDPA scan alone must not manufacture a verification score.
    assert VendorScoreEngine.calculate_compliance_score(test_db, v.id) == 0
