"""Regression: a scoring-formula change must not render as a vendor's decline.

``ScoreSnapshot`` is an immutable history, so when the formula changes the next
snapshot legitimately steps down. The "vs last cycle" delta shown in the
dashboard, the status snapshot PDF, the Vendor Pro report and the digest email
would attribute that step to the vendor.

Snapshots carry ``score_version`` (``vendor_status.SCORE_VERSION``). Consumers
compare it across the two most recent snapshots and suppress the delta when it
differs. History itself is left untouched — nothing is backfilled or rewritten.
"""
from datetime import datetime, timedelta, timezone

from app.core.models import ScoreSnapshot, User
from app.services.vendor_active_insights import get_score_trend
from app.services.vendor_status import SCORE_VERSION


def _vendor(db, email):
    u = User(email=email, hashed_password="x", role="VENDOR",
             plan="vendor_active", company="Vendor Pte Ltd")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _snapshot(db, vendor, final, version, age_days, compliance=50):
    db.add(ScoreSnapshot(
        vendor_id=vendor.id,
        base_score=float(final),
        multiplier=1.0,
        final_score=final,
        breakdown={"compliance": compliance},
        sector_percentile=50.0,
        score_version=version,
        snapshot_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    ))
    db.commit()


def test_delta_is_suppressed_across_a_formula_change(test_db):
    """The incident shape: the drop is ours, so no delta may be shown."""
    v = _vendor(test_db, "v+ver1@booppa.io")
    _snapshot(test_db, v, final=76, version="1.0", age_days=30, compliance=76)
    _snapshot(test_db, v, final=69, version="1.1", age_days=1, compliance=69)

    trend = get_score_trend(test_db, v.id)
    assert trend["total"] == 69          # the current score is still reported
    assert trend["total_delta"] is None  # ...but not as a 7-point fall
    assert trend["compliance_delta"] is None


def test_delta_is_reported_within_one_version(test_db):
    """Real movement must still surface — suppression is not a blanket mute."""
    v = _vendor(test_db, "v+ver2@booppa.io")
    _snapshot(test_db, v, final=60, version="1.1", age_days=30, compliance=55)
    _snapshot(test_db, v, final=68, version="1.1", age_days=1, compliance=63)

    trend = get_score_trend(test_db, v.id)
    assert trend["total_delta"] == 8
    assert trend["compliance_delta"] == 8


def test_first_snapshot_has_no_delta(test_db):
    v = _vendor(test_db, "v+ver3@booppa.io")
    _snapshot(test_db, v, final=60, version="1.1", age_days=1)

    trend = get_score_trend(test_db, v.id)
    assert trend["total"] == 60
    assert trend["total_delta"] is None


def test_snapshots_are_stamped_with_the_current_version(test_db):
    """Pins the writer to the constant, so a bump actually reaches new rows."""
    assert SCORE_VERSION == "1.1"

    import inspect

    from app.services import vendor_status

    src = inspect.getsource(vendor_status)
    # The literal must not be hardcoded at the write site again.
    assert "score_version    = SCORE_VERSION" in src
