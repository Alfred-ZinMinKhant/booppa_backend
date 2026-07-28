"""The bulk CSV importer is the widest inlet for unverified company identity.

A UEN typed into a spreadsheet is free text, but it is also a *real* UEN belonging
to *some* company — so if it were trusted, `fetch_acra_status` would return a
genuine registry record for the wrong legal person and the resulting report would
look entirely correct. That is the SPQR failure shape arriving 500 rows at a time.

These tests pin the import-time contract:
  1. A company row links a verified `ManagedEntity` on an ACRA match.
  2. A company ACRA doesn't know imports unverified — never guessed.
  3. A CSV UEN contradicting ACRA is dropped, not stamped, and does not abort
     the import of the other rows.
  4. Individuals get no company identity.
  5. Repeated company names reuse one entity rather than duplicating it.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.models import CspClient, ManagedEntity, User
from app.services.csp_bulk_import import execute_import, parse_csv

ALPHA = ("ALPHA HOLDINGS PTE. LTD.", "201234567A")
BRAVO = ("BRAVO VENTURES PTE. LTD.", "209876543B")


@pytest.fixture
def acra_stub(monkeypatch):
    """Keyed by company name, so a resolve returning the wrong company's UEN can
    only have come from trusting the CSV cell."""
    registry = {ALPHA[0].upper(): ALPHA, BRAVO[0].upper(): BRAVO}

    async def _fetch(company_name=None, uen=None, **_kw):
        if company_name and company_name.upper() in registry:
            name, found = registry[company_name.upper()]
            return {"found": True, "registered_name": name, "uen": found}
        return {"found": False}

    monkeypatch.setattr("app.services.managed_entity_service.fetch_acra_status", _fetch)
    return registry


@pytest.fixture
def owner(test_db):
    """The importing CSP firm. Its own identity is fully populated precisely so
    the assertions can prove no imported client ever borrows from it."""
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
    return user


@pytest.fixture
def csp_profile(test_db, owner):
    """`csp_clients.csp_id` is a real FK, so the import needs a real firm row."""
    from app.core.models import CspProfile
    from app.services.csp_access import activate_csp_access

    org = activate_csp_access(test_db, user=owner, plan="csp", billing_type="one_time")
    profile = CspProfile(
        organisation_id=org.id,
        legal_name=owner.legal_name,
        uen=owner.uen,
    )
    test_db.add(profile)
    test_db.commit()
    return profile


def _csv(*rows: str) -> bytes:
    header = "client_type,legal_name,uen_or_reg_no\n"
    return (header + "\n".join(rows) + "\n").encode("utf-8")


async def _import(test_db, owner, csp_profile, csv_bytes):
    rows, file_errors = parse_csv(csv_bytes)
    assert not file_errors
    return await execute_import(
        rows=rows,
        csp_id=str(csp_profile.id),
        db=test_db,
        owner_user_id=str(owner.id),
    )


def _client(test_db, created_id) -> CspClient:
    return test_db.query(CspClient).filter(CspClient.id == uuid.UUID(created_id)).one()


@pytest.mark.asyncio
async def test_matched_company_imports_verified(test_db, acra_stub, owner, csp_profile):
    result = await _import(test_db, owner, csp_profile, _csv(f"company,{ALPHA[0]},{ALPHA[1]}"))

    assert result.imported_count == 1
    assert result.identity_verified_count == 1
    assert result.identity_unverified_count == 0

    client = _client(test_db, result.created_ids[0])
    entity = test_db.query(ManagedEntity).get(client.managed_entity_id)
    assert entity.uen == ALPHA[1]
    assert entity.verified_at is not None


@pytest.mark.asyncio
async def test_unknown_company_imports_unverified_not_guessed(test_db, acra_stub, owner, csp_profile):
    result = await _import(test_db, owner, csp_profile, _csv("company,GHOST TRADING PTE LTD,999999999Z"))

    assert result.imported_count == 1, "an unverifiable company is still a valid AML record"
    assert result.identity_verified_count == 0
    assert result.identity_unverified_count == 1

    client = _client(test_db, result.created_ids[0])
    entity = test_db.query(ManagedEntity).get(client.managed_entity_id)
    assert entity.uen is None, "the CSV's UEN must never become identity"
    assert entity.legal_name is None
    assert entity.verified_at is None


@pytest.mark.asyncio
async def test_contradicted_uen_is_dropped_and_does_not_abort_import(
    test_db, acra_stub, owner, csp_profile
):
    """BRAVO's row claims ALPHA's UEN — the exact leak. It must import (an AML
    file is still required for a mistyped client) but must not resolve to ALPHA,
    and must not take the valid row down with it."""
    result = await _import(
        test_db,
        owner,
        csp_profile,
        _csv(f"company,{BRAVO[0]},{ALPHA[1]}", f"company,{ALPHA[0]},{ALPHA[1]}"),
    )

    assert result.imported_count == 2
    assert result.identity_verified_count == 1, "only ALPHA's own row verifies"

    bravo = _client(test_db, result.created_ids[0])
    entity = test_db.query(ManagedEntity).get(bravo.managed_entity_id)
    assert entity.company_name == BRAVO[0]
    assert entity.uen is None, "contradicted UEN must never be persisted"
    assert entity.legal_name != ALPHA[0]

    assert any(ALPHA[1] in w.get("message", "") for w in result.warnings), \
        "the CSP must be told which cell contradicted the registry"


@pytest.mark.asyncio
async def test_individual_gets_no_company_identity(test_db, acra_stub, owner, csp_profile):
    result = await _import(test_db, owner, csp_profile, _csv("individual,John Smith,"))

    assert result.imported_count == 1
    assert result.identity_individual_count == 1
    assert result.identity_verified_count == 0

    client = _client(test_db, result.created_ids[0])
    assert client.managed_entity_id is None
    assert test_db.query(ManagedEntity).filter(
        ManagedEntity.owner_user_id == owner.id
    ).count() == 0


@pytest.mark.asyncio
async def test_repeated_company_reuses_one_entity(test_db, acra_stub, owner, csp_profile):
    result = await _import(
        test_db,
        owner,
        csp_profile,
        _csv(f"company,{ALPHA[0]},{ALPHA[1]}", f"company,{ALPHA[0]},"),
    )

    assert result.imported_count == 2
    entities = (
        test_db.query(ManagedEntity)
        .filter(ManagedEntity.owner_user_id == owner.id)
        .all()
    )
    assert len(entities) == 1, "one company, one identity row"
    ids = {_client(test_db, cid).managed_entity_id for cid in result.created_ids}
    assert ids == {entities[0].id}


@pytest.mark.asyncio
async def test_no_owner_means_no_identity_rather_than_csv_uen(test_db, acra_stub, owner, csp_profile):
    """Callers that cannot supply an owner (older call sites) must degrade to
    *no* identity — never to the unchecked spreadsheet value."""
    rows, _ = parse_csv(_csv(f"company,{ALPHA[0]},{ALPHA[1]}"))
    result = await execute_import(
        rows=rows, csp_id=str(csp_profile.id), db=test_db, owner_user_id=None
    )

    assert result.imported_count == 1
    assert _client(test_db, result.created_ids[0]).managed_entity_id is None
    assert test_db.query(ManagedEntity).count() == 0
