"""Regression tests for ReportRepository's two session lookups.

A bundle checkout can create several stub Reports under one stripe session_id.
`list_by_stripe_session_id` and `get_by_stripe_session_id` used to share a
name, so the list version was silently shadowed and every list call site
(admin test-checkout) raised TypeError. These tests lock the two semantics.
"""

import uuid
from datetime import datetime, timedelta

from app.core.models import Report
from app.core.repositories.report_repository import ReportRepository


def _make_report(db, session_id: str, framework: str, created_at: datetime) -> Report:
    report = Report(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        framework=framework,
        company_name="Booppa QA",
        assessment_data={"stripe_session_id": session_id},
        status="pending",
        created_at=created_at,
    )
    db.add(report)
    db.commit()
    return report


def test_list_returns_every_stub_for_the_session(test_db):
    session_id = f"cs_test_{uuid.uuid4().hex[:16]}"
    base = datetime.utcnow()
    oldest = _make_report(test_db, session_id, "pdpa", base - timedelta(minutes=2))
    middle = _make_report(test_db, session_id, "vendor_trust", base - timedelta(minutes=1))
    newest = _make_report(test_db, session_id, "cover_sheet", base)
    # Different session — must not leak into either lookup.
    _make_report(test_db, f"cs_test_{uuid.uuid4().hex[:16]}", "pdpa", base)

    rows = ReportRepository.list_by_stripe_session_id(test_db, session_id)

    assert isinstance(rows, list)
    assert {str(r.id) for r in rows} == {str(oldest.id), str(middle.id), str(newest.id)}
    # Newest first, deterministically.
    assert [str(r.id) for r in rows] == [str(newest.id), str(middle.id), str(oldest.id)]


def test_get_returns_only_the_most_recent(test_db):
    session_id = f"cs_test_{uuid.uuid4().hex[:16]}"
    base = datetime.utcnow()
    _make_report(test_db, session_id, "pdpa", base - timedelta(minutes=2))
    newest = _make_report(test_db, session_id, "cover_sheet", base)

    row = ReportRepository.get_by_stripe_session_id(test_db, session_id)

    assert isinstance(row, Report)
    assert str(row.id) == str(newest.id)


def test_no_match_returns_empty_list_and_none(test_db):
    missing = f"cs_test_{uuid.uuid4().hex[:16]}"

    assert ReportRepository.list_by_stripe_session_id(test_db, missing) == []
    assert ReportRepository.get_by_stripe_session_id(test_db, missing) is None


def test_the_two_lookups_are_distinct_methods():
    """Guards against re-introducing the shadowed duplicate definition."""
    list_fn = ReportRepository.list_by_stripe_session_id
    get_fn = ReportRepository.get_by_stripe_session_id
    assert list_fn is not get_fn
    assert list_fn.__name__ != get_fn.__name__
