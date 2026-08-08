"""90-day post-cancellation read-only grace.

booppa-nextjs/app/pricing/page.tsx:24 promises buyers that "historical evidence
and certificates remain accessible for 90 days after cancellation". Before
2026-08-08 nothing recorded when a subscription lapsed, so the promise had no
implementation at all — a cancelled customer lost access the moment the webhook
fired.

These tests pin the two halves that can each silently restate the bug: the
boundary (89 in, 91 out — the number is a public commitment, not a tunable), and
the confinement to reads (grace must never reopen generation, seats or SSO).
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app.api.enterprise_api import _org_owner_plan, _require_suite_plan
from app.billing.enforcement import GRACE_WINDOW_DAYS, grace_plan_for_read
from app.core.models import Organisation, OrganisationMember, User
from fastapi import HTTPException


def _user(db, plan="free", lapsed_days=None, lapsed_plan=None):
    u = User(
        id=uuid.uuid4(),
        email=f"u-{uuid.uuid4().hex[:8]}@x.com",
        hashed_password="x",
        plan=plan,
    )
    if lapsed_days is not None:
        u.plan_lapsed_at = datetime.utcnow() - timedelta(days=lapsed_days)
        u.lapsed_plan = lapsed_plan or "pro_suite"
    db.add(u)
    db.flush()
    return u


def _org(db, owner):
    o = Organisation(
        id=uuid.uuid4(),
        name="Acme Pte Ltd",
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        owner_user_id=owner.id,
    )
    db.add(o)
    db.flush()
    db.add(OrganisationMember(id=uuid.uuid4(), organisation_id=o.id,
                              user_id=owner.id, role="owner"))
    db.commit()
    return o


# ── The boundary is a public promise ─────────────────────────────────────────

def test_grace_is_exactly_ninety_days():
    """Pinning the constant, not just the behaviour: changing it here without
    changing the pricing-page copy makes the product lie again."""
    assert GRACE_WINDOW_DAYS == 90


def test_day_eightynine_still_reads(test_db):
    u = _user(test_db, lapsed_days=89, lapsed_plan="pro_suite")
    assert grace_plan_for_read(u) == "pro_suite"


def test_day_ninetyone_is_denied(test_db):
    u = _user(test_db, lapsed_days=91, lapsed_plan="pro_suite")
    assert grace_plan_for_read(u) is None


def test_no_lapse_recorded_grants_nothing(test_db):
    """Users who lapsed before this column existed have no recorded date. They
    get no window — inventing one starting at migration time would be worse
    than the bug."""
    assert grace_plan_for_read(_user(test_db, plan="free")) is None


def test_lapsed_date_without_a_tier_grants_nothing(test_db):
    u = _user(test_db, plan="free")
    u.plan_lapsed_at = datetime.utcnow() - timedelta(days=1)
    u.lapsed_plan = None
    assert grace_plan_for_read(u) is None


# ── Confinement to reads ─────────────────────────────────────────────────────

def test_read_gate_admits_a_recently_lapsed_owner(test_db):
    owner = _user(test_db, plan="free", lapsed_days=10, lapsed_plan="pro_suite")
    org = _org(test_db, owner)
    assert _require_suite_plan(str(org.id), owner, test_db, read_only=True) is not None


def test_write_gate_refuses_the_same_owner(test_db):
    """The whole point of the parameter: grace is access to evidence already
    paid for, not three further months of the product."""
    owner = _user(test_db, plan="free", lapsed_days=10, lapsed_plan="pro_suite")
    org = _org(test_db, owner)
    with pytest.raises(HTTPException) as exc:
        _require_suite_plan(str(org.id), owner, test_db)
    assert exc.value.status_code == 402


def test_grace_does_not_promote_a_tier(test_db):
    """A lapsed Standard subscriber gets Standard back, not Pro. SSO is
    pro_only, so a grace path that ignored the stored tier would hand a
    cancelled Standard org an authentication feature it never bought."""
    owner = _user(test_db, plan="free", lapsed_days=10, lapsed_plan="standard_suite")
    org = _org(test_db, owner)
    assert _org_owner_plan(org, test_db, read_only=True) == "standard_suite"
    with pytest.raises(HTTPException) as exc:
        _require_suite_plan(str(org.id), owner, test_db, pro_only=True, read_only=True)
    assert exc.value.status_code == 402


def test_an_active_plan_is_never_overridden_by_a_stale_lapse(test_db):
    """Belt and braces against the re-subscription clear failing: a paying
    plan must win over whatever the lapse columns hold."""
    owner = _user(test_db, plan="standard_suite", lapsed_days=10, lapsed_plan="pro_suite")
    org = _org(test_db, owner)
    assert _org_owner_plan(org, test_db, read_only=True) == "standard_suite"


def test_default_gate_is_unchanged_for_active_subscribers(test_db):
    owner = _user(test_db, plan="pro_suite")
    org = _org(test_db, owner)
    assert _require_suite_plan(str(org.id), owner, test_db) is not None
