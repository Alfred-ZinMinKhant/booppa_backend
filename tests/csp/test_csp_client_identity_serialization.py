"""The client API must report entered-vs-verified identity as two separate things.

`uen_or_reg_no` is an AML case-file field: typed by the CSP or pasted from a CSV,
never checked. `identity_*` is what ACRA confirmed. If the API collapses them into
one field the UI has no way to tell them apart, and an unverified value renders
exactly like a verified one — which is the whole failure mode this work exists to
close.

The load-bearing assertion is `test_unverified_client_does_not_borrow_entered_uen`:
a client carrying a plausible free-text UEN must still serialize `identity_uen=None`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.csp import _serialize_client
from app.core.models import CspClient, CspProfile, ManagedEntity, User
from app.services.csp_access import activate_csp_access


@pytest.fixture
def owner(test_db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        legal_name="BOOPPA CORPORATE SERVICES PTE. LTD.",
        uen="199900002B",
    )
    test_db.add(user)
    test_db.commit()
    return user


@pytest.fixture
def csp_profile(test_db, owner):
    org = activate_csp_access(test_db, user=owner, plan="csp", billing_type="one_time")
    profile = CspProfile(
        organisation_id=org.id, legal_name=owner.legal_name, uen=owner.uen
    )
    test_db.add(profile)
    test_db.commit()
    return profile


def _client(test_db, csp_profile, *, entity=None, client_type="company", uen="201234567A"):
    row = CspClient(
        csp_id=csp_profile.id,
        client_type=client_type,
        legal_name="ALPHA HOLDINGS PTE. LTD.",
        uen_or_reg_no=uen,
        managed_entity_id=entity.id if entity else None,
    )
    test_db.add(row)
    test_db.commit()
    test_db.refresh(row)
    return row


def test_verified_client_reports_registry_name_and_uen(test_db, owner, csp_profile):
    entity = ManagedEntity(
        owner_user_id=owner.id,
        company_name="ALPHA HOLDINGS PTE. LTD.",
        legal_name="ALPHA HOLDINGS PTE. LTD.",
        uen="201234567A",
        verified_at=datetime.now(timezone.utc),
        status="ACTIVE",
    )
    test_db.add(entity)
    test_db.commit()

    data = _serialize_client(_client(test_db, csp_profile, entity=entity))

    assert data["identity_verified"] is True
    assert data["identity_uen"] == "201234567A"
    assert data["identity_legal_name"] == "ALPHA HOLDINGS PTE. LTD."
    assert data["identity_verified_at"] is not None


def test_unverified_client_does_not_borrow_entered_uen(test_db, owner, csp_profile):
    """An entity row exists but ACRA never confirmed it. The client's own
    free-text UEN is plausible and must stay quarantined in `uen_or_reg_no`."""
    entity = ManagedEntity(
        owner_user_id=owner.id,
        company_name="ALPHA HOLDINGS PTE. LTD.",
        status="ACTIVE",
    )
    test_db.add(entity)
    test_db.commit()

    data = _serialize_client(_client(test_db, csp_profile, entity=entity))

    assert data["identity_verified"] is False
    assert data["identity_uen"] is None, "an unchecked UEN must never read as identity"
    assert data["identity_legal_name"] is None
    assert data["uen_or_reg_no"] == "201234567A", "still available as an AML field"


def test_entity_with_uen_but_no_verified_at_is_not_verified(test_db, owner, csp_profile):
    """Verified means a confirmed lookup happened, not merely that a UEN is present.
    Without `verified_at` there is no provenance, so the value is untrusted."""
    entity = ManagedEntity(
        owner_user_id=owner.id,
        company_name="ALPHA HOLDINGS PTE. LTD.",
        uen="201234567A",
        status="ACTIVE",
    )
    test_db.add(entity)
    test_db.commit()

    data = _serialize_client(_client(test_db, csp_profile, entity=entity))

    assert data["identity_verified"] is False
    assert data["identity_uen"] is None


def test_client_with_no_entity_reports_unverified(test_db, owner, csp_profile):
    data = _serialize_client(_client(test_db, csp_profile))

    assert data["managed_entity_id"] is None
    assert data["identity_verified"] is False
    assert data["identity_uen"] is None
