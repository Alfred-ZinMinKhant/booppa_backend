"""Regression test: pdpa_monitor_monthly_rescan_task must actually run.

A local `from datetime import datetime, timezone, timedelta` placed mid-function
made those names local for the whole body, so the `datetime.now(timezone.utc)`
call seven lines earlier raised UnboundLocalError on *every* invocation. The
task's `except Exception` logged it and retried twice, so it failed silently —
taking out the monthly PDPA Monitor cron, the Vendor Pro quarterly scan,
subscription first-cycle deliverables, and the on-demand rescan endpoint.

The rest of the suite only asserted `.delay(...)` was called, which is why this
stayed green.
"""

import uuid

import pytest

from app.core.cache import cache as app_cache
from app.core.models import Report, User
from app.workers import tasks


@pytest.fixture
def monitor_user(test_db):
    user = User(
        id=uuid.uuid4(),
        email=f"monitor-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        company="Booppa QA",
        plan="pdpa_monitor",
    )
    test_db.add(user)
    test_db.commit()
    return user


def test_rescan_creates_stub_and_queues_followups(test_db, monitor_user, monkeypatch):
    queued: list[str] = []

    # Redis lock: grant it. Everything downstream is a queue call or an async
    # fulfillment run — stub them so the test exercises the task body only.
    # The task imports the cache locally, so patch it at its source module.
    monkeypatch.setattr(app_cache, "add", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(tasks, "asyncio", _StubAsyncio(), raising=False)
    for name in (
        "run_pdpa_monitor_report_for_user",
        "check_compliance_drift_task",
        "run_vendor_pro_pdpa_snapshot_for_user",
    ):
        monkeypatch.setattr(
            getattr(tasks, name),
            "apply_async",
            lambda *a, _n=name, **k: queued.append(_n),
            raising=False,
        )

    tasks.pdpa_monitor_monthly_rescan_task.run(
        str(monitor_user.id), monitor_user.email, "https://booppa.io"
    )

    stub = (
        test_db.query(Report)
        .filter(
            Report.owner_id == monitor_user.id,
            Report.framework == "pdpa_quick_scan",
        )
        .one()
    )
    assert stub.assessment_data["triggered_by"] == "pdpa_monitor_monthly"
    assert stub.assessment_data["contact_email"] == monitor_user.email
    # Monitor report + drift check both queued; no vendor_pro snapshot on this plan.
    assert queued == ["run_pdpa_monitor_report_for_user", "check_compliance_drift_task"]


def test_rescan_does_not_raise_unbound_local(test_db, monkeypatch):
    """Guards the specific failure mode: it used to blow up before any DB work."""
    # The task imports the cache locally, so patch it at its source module.
    monkeypatch.setattr(app_cache, "add", lambda *a, **k: True, raising=False)

    # Unknown vendor: the task should walk its body and fail on the uuid/queue
    # path at worst — never on a name lookup.
    try:
        tasks.pdpa_monitor_monthly_rescan_task.run(
            str(uuid.uuid4()), "nobody@example.com", "https://booppa.io"
        )
    except Exception as exc:  # celery Retry etc. are acceptable; UnboundLocalError is not
        assert not isinstance(exc, UnboundLocalError), exc
        assert "UnboundLocalError" not in repr(exc), exc


class _StubAsyncio:
    """Only `asyncio.run` is used inside the task body."""

    @staticmethod
    def run(coro):
        coro.close()
        return None
