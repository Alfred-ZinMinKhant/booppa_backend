"""Report-ready email must be sent at most once per report.

A single report can reach process_report_task more than once (scan→fulfill
chain, webhook standard-report branch, Celery retry). Before the dedupe guard
each run re-sent the "Your Audit Report is Ready" mail and the buyer received
duplicates (observed live: two identical mails ~60s apart). These tests lock the
guard in: one delivery per report, with the lock released on failure so a
legitimate retry can still get the mail out.
"""
import asyncio

import pytest


class _FakeRedis:
    """Minimal SETNX/delete redis stand-in."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


class _RecordingEmail:
    def __init__(self, result=True, raise_exc=None):
        self.calls = []
        self.result = result
        self.raise_exc = raise_exc

    async def send_report_ready_email(self, to_email, report_url, user_name, report_id):
        self.calls.append((to_email, report_url, user_name, report_id))
        if self.raise_exc:
            raise self.raise_exc
        return self.result


@pytest.fixture
def patched_redis(monkeypatch):
    from app.workers import tasks
    fake = _FakeRedis()
    monkeypatch.setattr(tasks.celery_app.backend, "client", fake, raising=False)
    return fake


def _run(coro):
    return asyncio.run(coro)


def test_second_send_for_same_report_is_skipped(patched_redis):
    from app.workers.tasks import _send_report_ready_email_once
    email = _RecordingEmail()

    first = _run(_send_report_ready_email_once(
        email, report_id="rep-1", to_email="a@x.com",
        report_url="https://dl/1", user_name="Acme"))
    second = _run(_send_report_ready_email_once(
        email, report_id="rep-1", to_email="a@x.com",
        report_url="https://dl/1", user_name="Acme"))

    assert first is True
    assert second is False           # deduped
    assert len(email.calls) == 1     # only one mail actually went out


def test_distinct_reports_each_get_their_own_email(patched_redis):
    from app.workers.tasks import _send_report_ready_email_once
    email = _RecordingEmail()

    _run(_send_report_ready_email_once(
        email, report_id="rep-A", to_email="a@x.com",
        report_url=None, user_name="A"))
    _run(_send_report_ready_email_once(
        email, report_id="rep-B", to_email="b@x.com",
        report_url=None, user_name="B"))

    assert {c[3] for c in email.calls} == {"rep-A", "rep-B"}
    assert len(email.calls) == 2


def test_provider_rejection_releases_lock_so_retry_can_resend(patched_redis):
    from app.workers.tasks import _send_report_ready_email_once
    # First attempt: provider rejects (returns False) → lock released.
    rejecting = _RecordingEmail(result=False)
    got = _run(_send_report_ready_email_once(
        rejecting, report_id="rep-2", to_email="a@x.com",
        report_url="https://dl/2", user_name="Acme"))
    assert got is False

    # Retry with a working provider must still be able to deliver.
    working = _RecordingEmail(result=True)
    got2 = _run(_send_report_ready_email_once(
        working, report_id="rep-2", to_email="a@x.com",
        report_url="https://dl/2", user_name="Acme"))
    assert got2 is True
    assert len(working.calls) == 1


def test_exception_releases_lock_and_propagates(patched_redis):
    from app.workers.tasks import _send_report_ready_email_once
    boom = _RecordingEmail(raise_exc=RuntimeError("smtp down"))
    with pytest.raises(RuntimeError):
        _run(_send_report_ready_email_once(
            boom, report_id="rep-3", to_email="a@x.com",
            report_url=None, user_name="Acme"))

    # Lock was released → a subsequent healthy send goes through.
    ok = _RecordingEmail(result=True)
    assert _run(_send_report_ready_email_once(
        ok, report_id="rep-3", to_email="a@x.com",
        report_url=None, user_name="Acme")) is True
    assert len(ok.calls) == 1


def test_redis_unavailable_still_sends(monkeypatch):
    """If Redis is down we must not silently drop the mail — fall back to send."""
    from app.workers import tasks

    class _BrokenRedis:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.celery_app.backend, "client", _BrokenRedis(), raising=False)
    email = _RecordingEmail()
    got = _run(tasks._send_report_ready_email_once(
        email, report_id="rep-4", to_email="a@x.com",
        report_url=None, user_name="Acme"))
    assert got is True
    assert len(email.calls) == 1
