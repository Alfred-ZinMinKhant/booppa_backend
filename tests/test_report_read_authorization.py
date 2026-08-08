"""Reading a report requires ownership, a share token, or an unowned report.

Until 2026-08-08 the check was `if report and current_user and
report.owner_id != current_user.id` — enforced only when credentials were
presented. An anonymous caller holding a report id read any customer's report,
and signing out turned a 403 into a 200 (AUDIT_2026-08-08.md P1-2).

These tests pin `_may_read_report` and the token scoping it relies on.
"""
import uuid
from datetime import timedelta
from types import SimpleNamespace

from app.api.reports import _may_read_report
from app.core.auth import create_report_share_token, verify_report_share_token


def _report(owner_id):
    return SimpleNamespace(id=uuid.uuid4(), owner_id=owner_id, assessment_data={})


def _user(uid):
    return SimpleNamespace(id=uid)


def test_owner_may_read():
    uid = uuid.uuid4()
    assert _may_read_report(_report(uid), _user(uid))


def test_other_signed_in_user_may_not_read():
    assert not _may_read_report(_report(uuid.uuid4()), _user(uuid.uuid4()))


def test_anonymous_may_not_read_an_owned_report():
    """The regression: no credentials used to mean no check at all."""
    assert not _may_read_report(_report(uuid.uuid4()), None)


def test_unowned_free_scan_stays_public():
    """`create_report_public` leaves owner_id NULL; those links must keep working."""
    assert _may_read_report(_report(None), None)


def test_missing_report_is_not_readable():
    assert not _may_read_report(None, _user(uuid.uuid4()))


# ── share tokens ──────────────────────────────────────────────────────────────

def test_share_token_admits_anonymous_reader():
    rep = _report(uuid.uuid4())
    token = create_report_share_token(str(rep.id))
    assert _may_read_report(rep, None, token)


def test_share_token_is_scoped_to_one_report():
    """A token for report A must not open report B."""
    other = create_report_share_token(str(uuid.uuid4()))
    assert not _may_read_report(_report(uuid.uuid4()), None, other)


def test_expired_share_token_is_rejected():
    rep = _report(uuid.uuid4())
    stale = create_report_share_token(str(rep.id), expires_delta=timedelta(seconds=-1))
    assert not _may_read_report(rep, None, stale)


def test_garbage_token_is_rejected():
    assert not _may_read_report(_report(uuid.uuid4()), None, "not-a-jwt")


def test_access_token_is_not_a_share_token():
    """Type discrimination both ways: an access token must not open a report."""
    from app.core.auth import create_access_token, verify_access_token

    rid = str(uuid.uuid4())
    assert not verify_report_share_token(create_access_token({"sub": "a@b.c"}), rid)
    # …and a share token must not authenticate a session.
    assert verify_access_token(create_report_share_token(rid)) is None
