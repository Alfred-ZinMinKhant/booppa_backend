"""What the board-report task records must match what the subscriber received.

This report is a paid monthly Suite deliverable that had never once executed in
production — its `TRM_BOARD_REPORT_SCHEMA_VERSION` ImportError was swallowed by a
broad `except`, so nothing errored and nothing ran
(`swallowed_exception_dead_feature`). Bringing it to life exposed four ways it
could report success it had not earned. These pin all four.

The PDF is delivered as an email attachment, not a link — so a rejected send is
not a degraded delivery, it is no delivery at all. `send_html_email` returns
False rather than raising (`email_service`), which is precisely the shape that
persisted `status="completed"` on a report nobody got.
"""
from __future__ import annotations

import inspect

import pytest

from app.core.models import Report
from app.workers import tasks
from tests._test_helpers import make_user


def _suite_user(db, email):
    u = make_user(db, email=email, plan="pro_suite", company="Acme Suite")
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def _stub_pipeline(monkeypatch):
    """Neutralise everything outside the delivery bookkeeping under test."""
    # The task imports the generator inside the function body, so the patch must
    # land on the source module, not on `tasks`.
    monkeypatch.setattr(
        "app.services.trm_board_report_generator.generate_trm_board_report_pdf",
        lambda ctx: b"%PDF-1.4 fake",
    )

    class _S3:
        async def upload_pdf(self, data, name):
            return f"https://s3.example/{name}.pdf"

    monkeypatch.setattr("app.services.storage.S3Service", _S3)


def _send_returns(monkeypatch, value: bool):
    class _Email:
        async def send_html_email(self, **kwargs):
            return value

    # `EmailService` is bound into the tasks namespace at import time — patching
    # `app.services.email_service.EmailService` would not be seen here.
    monkeypatch.setattr(tasks, "EmailService", _Email)


def _row(db, user):
    return (
        db.query(Report)
        .filter(Report.framework == "trm_board_report", Report.owner_id == user.id)
        .one_or_none()
    )


# ── 1. A rejected email must not persist as a completed delivery ─────────────

def test_rejected_email_is_not_recorded_as_completed(test_db, monkeypatch, _stub_pipeline):
    user = _suite_user(test_db, "board-reject@booppa.io")
    _send_returns(monkeypatch, False)

    tasks.run_trm_board_report_for_user(str(user.id))

    row = _row(test_db, user)
    assert row is not None, "the run must still leave evidence it happened"
    assert row.status == "delivery_failed"
    assert row.assessment_data["delivered"] is False
    # `completed_at` is what a stranded-fulfillment sweep reads as "done".
    assert row.completed_at is None


def test_accepted_email_is_recorded_as_completed(test_db, monkeypatch, _stub_pipeline):
    user = _suite_user(test_db, "board-ok@booppa.io")
    _send_returns(monkeypatch, True)

    tasks.run_trm_board_report_for_user(str(user.id))

    row = _row(test_db, user)
    assert row.status == "completed"
    assert row.assessment_data["delivered"] is True
    assert row.completed_at is not None


# ── 2. Retry exhaustion must reach a human ───────────────────────────────────

def test_exhaustion_routes_through_retry_or_alert():
    """A bare `self.retry` dies after max_retries with only a Celery log line —
    a paid monthly deliverable that vanishes until the subscriber complains."""
    src = inspect.getsource(tasks.run_trm_board_report_for_user)
    assert "_retry_or_alert" in src
    assert "raise self.retry" not in src


# ── 3. One bad subscriber must not cancel the rest of the month ──────────────

def test_fanout_continues_past_a_failing_subscriber(test_db, monkeypatch):
    """The handler used to wrap the whole loop: the first uid that could not be
    queued aborted every subscriber after it, and the task still returned
    normally with a queued count that looked like a complete month."""
    from app.core.models import Subscription

    users = [_suite_user(test_db, f"fanout-{i}@booppa.io") for i in range(3)]
    for i, u in enumerate(users):
        test_db.add(Subscription(
            user_id=u.id, product_type="pro_suite_monthly", status="active",
            stripe_subscription_id=f"sub_fanout_{i}",
        ))
    test_db.commit()

    seen, boom = [], users[0].id

    def _delay(uid):
        if uid == str(boom):
            raise RuntimeError("broker refused this one")
        seen.append(uid)

    monkeypatch.setattr(tasks.run_trm_board_report_for_user, "delay", _delay)

    result = tasks.run_trm_monthly_board_reports()

    assert result["failed"] == 1
    assert result["queued"] >= 2, "subscribers after the failure were skipped"
    assert str(boom) not in seen


# ── 4. A broken deploy must not render as "no PDPA scan" ─────────────────────

def test_pdpa_imports_are_outside_the_tolerant_block():
    """The `except` there is deliberate — an absent scan renders Not Assessed.

    But an ImportError from `app.services.pdpa_findings` is a broken deploy, and
    caught in the same place it renders the identical clean Not Assessed section.
    That is the exact shape that left this task dead in production while looking
    healthy, so the imports must sit above the `try`.
    """
    src = inspect.getsource(tasks.run_trm_board_report_for_user)
    import_at = src.index("from app.services.pdpa_findings import")
    try_at = src.index("scans = latest_vendor_pdpa_reports")
    guarded_try = src.rindex("try:", 0, try_at)
    assert import_at < guarded_try, "PDPA imports moved back inside the try"
