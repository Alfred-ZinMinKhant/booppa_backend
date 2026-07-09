"""Retention pruning for the append-only search-impression log.

`cleanup_old_tasks` (hourly beat job) must delete SearchImpression rows older
than 90 days so the table can't grow unbounded, while leaving recent rows — the
ones get_search_impressions_30d reads — untouched.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

import app.workers.tasks as tasks_mod
from app.core.models import SearchImpression


def test_cleanup_prunes_only_stale_impressions(test_db, monkeypatch):
    # Pin the task's own session factory to the test engine so the standalone
    # SessionLocal it opens hits the same DB as `test_db`, regardless of env.
    monkeypatch.setattr(
        tasks_mod, "SessionLocal", sessionmaker(bind=test_db.get_bind())
    )

    vid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    recent = SearchImpression(vendor_id=vid, source="marketplace", created_at=now)
    edge = SearchImpression(
        vendor_id=vid, source="discovery", created_at=now - timedelta(days=29)
    )
    stale = SearchImpression(
        vendor_id=vid, source="discovery", created_at=now - timedelta(days=100)
    )
    test_db.add_all([recent, edge, stale])
    test_db.commit()
    assert test_db.query(SearchImpression).count() == 3

    # Run the hourly cleanup job (callable synchronously).
    tasks_mod.cleanup_old_tasks()

    test_db.expire_all()
    rows = test_db.query(SearchImpression).all()
    # The 100-day-old row is gone; the recent and 29-day rows survive.
    assert len(rows) == 2
    ages_ok = all((now - r.created_at.replace(tzinfo=timezone.utc)).days < 90 for r in rows)
    assert ages_ok
