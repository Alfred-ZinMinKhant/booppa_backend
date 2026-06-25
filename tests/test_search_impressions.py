"""Vendor Active search-impression logging + monthly count."""
import uuid
from datetime import datetime, timedelta, timezone

from app.core.models_v10 import SearchImpression
from app.services.marketplace import record_search_impressions
from app.services.vendor_active_insights import get_search_impressions_30d


def test_record_dedups_per_vendor(test_db):
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    record_search_impressions(test_db, [v1, v1, v2, None], "marketplace", "acme")
    rows = test_db.query(SearchImpression).all()
    assert len(rows) == 2
    assert {str(r.vendor_id) for r in rows} == {str(v1), str(v2)}
    assert all(r.source == "marketplace" for r in rows)


def test_count_window_is_trailing_30d(test_db):
    vid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    # one recent, one stale (40 days ago)
    test_db.add(SearchImpression(vendor_id=vid, source="discovery", created_at=now))
    test_db.add(SearchImpression(vendor_id=vid, source="discovery", created_at=now - timedelta(days=40)))
    test_db.commit()
    assert get_search_impressions_30d(test_db, vid) == 1


def test_no_impressions_returns_zero(test_db):
    assert get_search_impressions_30d(test_db, uuid.uuid4()) == 0


def test_record_is_best_effort_and_never_raises():
    # A broken session must not propagate — search itself must keep working.
    record_search_impressions(None, [uuid.uuid4()], "marketplace", "q")
