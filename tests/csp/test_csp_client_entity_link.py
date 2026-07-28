"""`csp_clients` must not be a second, unverified source of report identity.

Before the `managed_entity_id` link (2026_07_28_0003), a CSP's client roster lived
in `csp_clients` with a free-text `uen_or_reg_no` that no identity resolver read
and nothing ever checked against ACRA — while the verified store
(`managed_entities`) sat empty because no surface wrote to it.

The failure that made this urgent: a UEN typed into the bulk-import CSV is a real
UEN belonging to *some* company, so `fetch_acra_status` returns a genuine registry
record for the wrong legal person, and the resulting document looks entirely
correct. Same shape as the SPQR leak, one layer over.

These tests pin the properties that close it:
  1. Creating a company client links a `ManagedEntity`.
  2. An unmatched company links an UNVERIFIED entity — never a guess.
  3. A UEN contradicting ACRA is dropped, not stamped, and never blocks CDD.
  4. An `individual` client gets no company identity at all.
  5. A backfilled entity does not inherit trust from the free-text UEN.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.models import ManagedEntity, User
from app.services.evidence_enricher import MANAGED_ENTITY_CARRIER, trusted_cached_uen
from app.services.managed_entity_service import (
    UenMismatch,
    resolve_or_create_managed_entity,
)

# ACRA says this UEN belongs to ALPHA. A CSP typing it against BRAVO is the bug.
ALPHA = ("ALPHA HOLDINGS PTE. LTD.", "201234567A")
BRAVO = ("BRAVO VENTURES PTE. LTD.", "209876543B")


@pytest.fixture
def acra_stub(monkeypatch):
    """Registry keyed by company name, so a resolve returning the wrong company's
    UEN can only have come from trusting an unverified claim."""
    registry = {ALPHA[0].upper(): ALPHA, BRAVO[0].upper(): BRAVO}

    async def _fetch(company_name=None, uen=None, **_kw):
        if company_name and company_name.upper() in registry:
            name, found_uen = registry[company_name.upper()]
            return {"found": True, "registered_name": name, "uen": found_uen}
        if uen:
            for name, found_uen in registry.values():
                if found_uen.upper() == uen.upper():
                    return {"found": True, "registered_name": name, "uen": found_uen}
        return {"found": False}

    monkeypatch.setattr(
        "app.services.managed_entity_service.fetch_acra_status", _fetch
    )
    return registry


@pytest.fixture
def owner_id(test_db):
    """A real CSP firm account. Its own identity is fully populated precisely so the
    assertions can prove no client entity ever borrows from it."""
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        legal_name="BOOPPA CORPORATE SERVICES PTE. LTD.",
        legal_name_hint="BOOPPA CORPORATE SERVICES PTE. LTD.",
        uen="199900002B",
    )
    test_db.add(user)
    test_db.commit()
    return user.id


@pytest.mark.asyncio
async def test_company_client_links_verified_entity(test_db, acra_stub, owner_id):
    result = await resolve_or_create_managed_entity(
        test_db, owner_user_id=owner_id, company_name=ALPHA[0], claimed_uen=ALPHA[1]
    )
    assert result.created is True
    assert result.verified is True
    assert result.entity.uen == ALPHA[1]
    assert result.entity.verified_at is not None
    assert result.mismatch is None


@pytest.mark.asyncio
async def test_unmatched_company_is_unverified_never_guessed(test_db, acra_stub, owner_id):
    """A company ACRA has never heard of must persist with NULL identity — the
    state downstream readers render "Not verified"."""
    result = await resolve_or_create_managed_entity(
        test_db, owner_user_id=owner_id, company_name="GHOST TRADING PTE LTD"
    )
    assert result.created is True
    assert result.verified is False
    assert result.entity.uen is None
    assert result.entity.legal_name is None
    assert result.entity.verified_at is None


@pytest.mark.asyncio
async def test_contradicted_uen_is_dropped_not_stamped(test_db, acra_stub, owner_id):
    """BRAVO claiming ALPHA's UEN is the exact leak shape.

    Non-strict (the AML path) must still create the CDD-backing row, but must NOT
    resolve it to ALPHA. Stamping ALPHA's registration onto BRAVO's document is the
    failure; refusing to choose is the fix.
    """
    result = await resolve_or_create_managed_entity(
        test_db,
        owner_user_id=owner_id,
        company_name=BRAVO[0],
        claimed_uen=ALPHA[1],
        strict=False,
    )
    assert result.created is True
    assert result.mismatch is not None
    assert result.verified is False
    assert result.entity.uen is None, "contradicted UEN must never be persisted"
    assert result.entity.legal_name != ALPHA[0]
    assert ALPHA[1] not in (result.entity.uen or "")


@pytest.mark.asyncio
async def test_contradicted_uen_is_rejected_when_strict(test_db, acra_stub, owner_id):
    """On the direct-registration form the customer is looking at the field, so a
    contradiction is a correction to make now — not a row to accept unverified."""
    with pytest.raises(UenMismatch):
        await resolve_or_create_managed_entity(
            test_db,
            owner_user_id=owner_id,
            company_name=BRAVO[0],
            claimed_uen=ALPHA[1],
            strict=True,
        )


@pytest.mark.asyncio
async def test_existing_entity_is_reused_not_duplicated(test_db, acra_stub, owner_id):
    first = await resolve_or_create_managed_entity(
        test_db, owner_user_id=owner_id, company_name=ALPHA[0], claimed_uen=ALPHA[1]
    )
    second = await resolve_or_create_managed_entity(
        test_db, owner_user_id=owner_id, company_name=ALPHA[0]
    )
    assert second.created is False
    assert second.entity.id == first.entity.id
    rows = (
        test_db.query(ManagedEntity)
        .filter(ManagedEntity.owner_user_id == owner_id)
        .all()
    )
    assert len(rows) == 1


def test_backfilled_entity_does_not_inherit_free_text_trust(test_db, owner_id):
    """A backfilled row carries no provenance, so its identity is untrusted even if
    a UEN is later present. This is what stops the old CSV values from being
    laundered into verified identity."""
    row = ManagedEntity(
        id=uuid.uuid4(),
        owner_user_id=owner_id,
        company_name=BRAVO[0],
        # Simulates the shape a naive backfill would have produced: a UEN copied
        # from `csp_clients.uen_or_reg_no` with no verification behind it.
        uen=ALPHA[1],
        verified_hint=None,
        verified_at=None,
        status="ACTIVE",
    )
    test_db.add(row)
    test_db.commit()

    assert (
        trusted_cached_uen(
            row, company_hint=BRAVO[0], carrier=MANAGED_ENTITY_CARRIER
        )
        is None
    ), "an unprovenanced UEN must never be trusted, even sitting in the identity table"
