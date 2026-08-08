"""Retention purge worker — the enforcement behind RetentionPolicy.

Before 2026-08-08 `RetentionPolicy` rows were written, read back to the
customer, and never acted on, while the public privacy page promised deletion on
expiry. These tests pin the three things that would quietly restate that bug:
the cutoff arithmetic (over-deleting is irreversible), the scoping (one org must
never purge another's rows, and accepted invites are membership records), and
the refusal to store a category nothing can enforce.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app.core.models import (
    Organisation,
    OrganisationInvite,
    RetentionPolicy,
    RetentionPurgeLog,
    SlaLog,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services.retention import RETENTION_CATEGORIES, is_enforceable, purge_organisation_policies


def _make_org(db, name="Acme Pte Ltd"):
    owner = User(id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex[:8]}@x.com", hashed_password="x")
    db.add(owner)
    db.flush()
    o = Organisation(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.split()[0].lower()}-{uuid.uuid4().hex[:8]}",
        owner_user_id=owner.id,
    )
    db.add(o)
    db.flush()
    return o


@pytest.fixture
def org(test_db):
    o = _make_org(test_db)
    test_db.commit()
    return o


def _policy(org, category="sla_logs", days=30, auto=True):
    return RetentionPolicy(
        id=uuid.uuid4(),
        organisation_id=org.id,
        data_category=category,
        retention_days=days,
        auto_purge=auto,
    )


def _sla(org, age_days):
    return SlaLog(
        id=uuid.uuid4(),
        organisation_id=org.id,
        event_type="report_delivery",
        recorded_at=datetime.utcnow() - timedelta(days=age_days),
    )


# ── Cutoff arithmetic ────────────────────────────────────────────────────────

def test_purges_only_rows_older_than_the_retention_window(test_db, org):
    """The boundary is the whole point: 31 days old goes, 29 stays.

    An off-by-one here deletes a day of data the customer was promised, and
    there is no undo.
    """
    test_db.add_all([_sla(org, 31), _sla(org, 29), _sla(org, 100)])
    policy = _policy(org, days=30)
    test_db.add(policy)
    test_db.commit()

    log = purge_organisation_policies(test_db, policy)
    test_db.commit()

    assert log.rows_deleted == 2
    remaining = test_db.query(SlaLog).filter(SlaLog.organisation_id == org.id).all()
    assert len(remaining) == 1
    assert (datetime.utcnow() - remaining[0].recorded_at).days == 29


def test_dry_run_reports_what_it_would_delete_without_deleting(test_db, org):
    test_db.add_all([_sla(org, 60), _sla(org, 60)])
    policy = _policy(org, days=30)
    test_db.add(policy)
    test_db.commit()

    log = purge_organisation_policies(test_db, policy, dry_run=True)
    test_db.commit()

    assert log.rows_deleted == 2 and log.dry_run is True
    assert test_db.query(SlaLog).filter(SlaLog.organisation_id == org.id).count() == 2


def test_writes_an_audit_row_even_when_nothing_was_eligible(test_db, org):
    """"We checked and there was nothing to delete" is a different claim from
    "we never ran" — an auditor tests for the former."""
    test_db.add(_sla(org, 5))
    policy = _policy(org, days=30)
    test_db.add(policy)
    test_db.commit()

    purge_organisation_policies(test_db, policy)
    test_db.commit()

    logs = test_db.query(RetentionPurgeLog).filter(RetentionPurgeLog.organisation_id == org.id).all()
    assert len(logs) == 1 and logs[0].rows_deleted == 0


# ── Scoping ──────────────────────────────────────────────────────────────────

def test_one_organisations_policy_never_touches_anothers_rows(test_db, org):
    other = _make_org(test_db, "Other Ltd")
    test_db.add_all([_sla(org, 90), _sla(other, 90)])
    policy = _policy(org, days=30)
    test_db.add(policy)
    test_db.commit()

    purge_organisation_policies(test_db, policy)
    test_db.commit()

    assert test_db.query(SlaLog).filter(SlaLog.organisation_id == org.id).count() == 0
    assert test_db.query(SlaLog).filter(SlaLog.organisation_id == other.id).count() == 1


def test_webhook_deliveries_are_scoped_through_the_owning_endpoint(test_db, org):
    """WebhookDelivery has no organisation_id — it hangs off the endpoint. A
    purge that forgot the join would delete every org's delivery history."""
    other = _make_org(test_db, "Other Ltd")
    mine = WebhookEndpoint(id=uuid.uuid4(), organisation_id=org.id, url="https://a", secret="s")
    theirs = WebhookEndpoint(id=uuid.uuid4(), organisation_id=other.id, url="https://b", secret="s")
    test_db.add_all([mine, theirs])
    test_db.flush()
    old = datetime.utcnow() - timedelta(days=90)
    test_db.add_all([
        WebhookDelivery(id=uuid.uuid4(), endpoint_id=mine.id, event_type="report.ready", delivered_at=old),
        WebhookDelivery(id=uuid.uuid4(), endpoint_id=theirs.id, event_type="report.ready", delivered_at=old),
    ])
    policy = _policy(org, category="webhook_deliveries", days=30)
    test_db.add(policy)
    test_db.commit()

    log = purge_organisation_policies(test_db, policy)
    test_db.commit()

    assert log.rows_deleted == 1
    assert test_db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == theirs.id).count() == 1


def test_accepted_invites_survive_expiry(test_db, org):
    """An accepted invite records WHO admitted a member. Deleting it because a
    date passed erases the membership audit trail, not stale data."""
    inviter = User(id=uuid.uuid4(), email=f"i-{uuid.uuid4().hex[:6]}@x.com", hashed_password="x")
    test_db.add(inviter)
    test_db.flush()
    long_ago = datetime.utcnow() - timedelta(days=400)
    test_db.add_all([
        OrganisationInvite(id=uuid.uuid4(), organisation_id=org.id, email="a@x.com", token=uuid.uuid4().hex,
                           invited_by_user_id=inviter.id, status="accepted", expires_at=long_ago),
        OrganisationInvite(id=uuid.uuid4(), organisation_id=org.id, email="b@x.com", token=uuid.uuid4().hex,
                           invited_by_user_id=inviter.id, status="pending", expires_at=long_ago),
    ])
    policy = _policy(org, category="expired_invites", days=30)
    test_db.add(policy)
    test_db.commit()

    log = purge_organisation_policies(test_db, policy)
    test_db.commit()

    assert log.rows_deleted == 1
    survivors = test_db.query(OrganisationInvite).filter(OrganisationInvite.organisation_id == org.id).all()
    assert [s.status for s in survivors] == ["accepted"]


# ── Refusing to act on what it cannot enforce ────────────────────────────────

def test_policy_with_auto_purge_off_deletes_nothing(test_db, org):
    test_db.add(_sla(org, 90))
    policy = _policy(org, days=30, auto=False)
    test_db.add(policy)
    test_db.commit()

    assert purge_organisation_policies(test_db, policy) is None
    assert test_db.query(SlaLog).count() == 1


def test_legacy_free_text_category_is_skipped_not_guessed(test_db, org):
    """"personal_data" was the model's own suggested example and maps to no
    table. Skipping is right; guessing a target would delete data the customer
    never nominated."""
    test_db.add(_sla(org, 90))
    policy = _policy(org, category="personal_data", days=30)
    test_db.add(policy)
    test_db.commit()

    assert purge_organisation_policies(test_db, policy) is None
    assert test_db.query(SlaLog).count() == 1
    assert is_enforceable("personal_data") is False


def test_reports_are_not_a_purgeable_category(test_db):
    """Report is user-scoped (models.py owner_id), not org-scoped, and holds
    anchored evidence the customer paid for. No org policy may reach it."""
    assert "reports" not in RETENTION_CATEGORIES
    assert set(RETENTION_CATEGORIES) == {"sla_logs", "webhook_deliveries", "expired_invites"}
