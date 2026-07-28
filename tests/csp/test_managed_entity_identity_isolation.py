"""CSP/DPO managed-entity identity isolation: one login, many client companies.

The sibling of `tests/test_buyer_vendor_identity_isolation.py`. A buyer names its
subject by typing a vendor's name; a CSP names its subject by *selecting* one of
the client companies it manages. Same failure mode either way — the account's own
identity, or the previously-resolved client's, standing in for this order's subject
— so the same property is asserted here against `ManagedEntity` / the
`MANAGED_ENTITY_CARRIER` code path.

A CSP certifying its own firm under a client's order is the worst shape of this
bug: the document looks entirely correct and names the wrong legal person.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.models import ManagedEntity, User
from app.services.evidence_enricher import (
    MANAGED_ENTITY_CARRIER,
    resolve_managed_entity_identity,
    trusted_cached_uen,
)

CSP_COMPANY = "BOOPPA CORPORATE SERVICES PTE. LTD."
CSP_UEN = "199900002B"

CLIENT_A = ("NETPOLEONS TECHNOLOGY PTE LTD", "201234567A", "netpoleons.com")
CLIENT_B = ("SPQR COMMUNICATIONS PTE. LTD.", "21374700J", "spqr.example")


def _make_csp(db, company=CSP_COMPANY, uen=CSP_UEN, site="booppa-cs.example") -> User:
    """A CSP firm with a complete identity of its own — the thing that must never
    appear on a client company's document."""
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",  # a CSP firm IS a vendor account; the entity picker, not the
                        # role, is what makes its orders cross-entity here
        company=company,
        legal_name=company,
        legal_name_hint=company,
        uen=uen,
        website=site,
        website_hint=site,
    )
    db.add(user)
    db.commit()
    return user


def _add_entity(db, owner, name, uen=None, domain=None, verified_hint=None) -> ManagedEntity:
    row = ManagedEntity(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        company_name=name,
        legal_name=name if uen else None,
        uen=uen,
        website=f"https://{domain}" if domain else None,
        verified_hint=verified_hint,
        status="ACTIVE",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def acra_stub(monkeypatch):
    """Registry answers keyed by company name, so a resolve that returns the WRONG
    entity's data can only have come from reading the wrong row."""
    registry = {
        CLIENT_A[0].upper(): CLIENT_A,
        CLIENT_B[0].upper(): CLIENT_B,
        CSP_COMPANY.upper(): (CSP_COMPANY, CSP_UEN, "booppa-cs.example"),
    }

    from app.services import evidence_enricher

    async def _stub(uen=None, company_name=None):
        if company_name and company_name.upper() in registry:
            name, resolved_uen, _domain = registry[company_name.upper()]
            return {"found": True, "live": True, "uen": resolved_uen,
                    "registered_name": name, "entity_status": "LIVE"}
        if uen:
            for name, r_uen, _d in registry.values():
                if r_uen == uen:
                    return {"found": True, "live": True, "uen": r_uen,
                            "registered_name": name, "entity_status": "LIVE"}
        return {"found": False}

    monkeypatch.setattr(evidence_enricher, "fetch_acra_status", _stub)


# ── The core isolation property ───────────────────────────────────────────────


def test_two_client_entities_resolve_independently(test_db, acra_stub):
    csp = _make_csp(test_db)
    a = _add_entity(test_db, csp, CLIENT_A[0], domain=CLIENT_A[2])
    b = _add_entity(test_db, csp, CLIENT_B[0], domain=CLIENT_B[2])

    res_a = asyncio.run(resolve_managed_entity_identity(a, test_db))
    res_b = asyncio.run(resolve_managed_entity_identity(b, test_db))

    assert res_a["uen"] == CLIENT_A[1]
    assert res_b["uen"] == CLIENT_B[1]
    assert res_a["legal_name"] == CLIENT_A[0]
    assert res_b["legal_name"] == CLIENT_B[0]

    # Neither document borrows the managing firm's identity.
    for res in (res_a, res_b):
        assert res["uen"] != CSP_UEN
        assert res["legal_name"] != CSP_COMPANY


def test_resolution_order_does_not_leak_the_first_client(test_db, acra_stub):
    """Resolve A, then B — B must not inherit A's UEN."""
    csp = _make_csp(test_db)
    a = _add_entity(test_db, csp, CLIENT_A[0], domain=CLIENT_A[2])
    b = _add_entity(test_db, csp, CLIENT_B[0], domain=CLIENT_B[2])

    asyncio.run(resolve_managed_entity_identity(a, test_db))
    second = asyncio.run(resolve_managed_entity_identity(b, test_db))

    assert second["uen"] == CLIENT_B[1]
    assert second["uen"] != CLIENT_A[1]


def test_csp_account_is_never_touched_by_a_client_resolve(test_db, acra_stub):
    csp = _make_csp(test_db)
    entity = _add_entity(test_db, csp, CLIENT_A[0], domain=CLIENT_A[2])

    asyncio.run(resolve_managed_entity_identity(entity, test_db))

    test_db.refresh(csp)
    assert csp.uen == CSP_UEN
    assert csp.company == CSP_COMPANY
    assert csp.legal_name == CSP_COMPANY
    assert csp.legal_name_hint == CSP_COMPANY


# ── Write-back lands on the entity row, with provenance ───────────────────────


def test_verification_is_recorded_on_the_entity_row(test_db, acra_stub):
    csp = _make_csp(test_db)
    entity = _add_entity(test_db, csp, CLIENT_A[0], domain=CLIENT_A[2])

    asyncio.run(resolve_managed_entity_identity(entity, test_db))

    test_db.refresh(entity)
    assert entity.uen == CLIENT_A[1]
    assert entity.legal_name == CLIENT_A[0]
    # Provenance: what this identity was resolved FROM. Without it the cached UEN
    # is unprovenanced and must not be trusted on the next order.
    assert entity.verified_hint == CLIENT_A[0]
    assert entity.verified_at is not None


def test_cached_identity_is_not_trusted_for_a_different_subject(test_db):
    """The entity was renamed (or the hint is from another company): the cached UEN
    is stale and must not be handed to a document about the new name."""
    csp = _make_csp(test_db)
    entity = _add_entity(
        test_db, csp, CLIENT_B[0], uen=CLIENT_A[1], verified_hint=CLIENT_A[0],
    )

    assert trusted_cached_uen(
        entity, company_hint=CLIENT_B[0], carrier=MANAGED_ENTITY_CARRIER
    ) is None


def test_unprovenanced_entity_row_is_not_trusted(test_db):
    """A row carrying a UEN but no `verified_hint` — hand-entered or imported —
    never earns trust just by existing."""
    csp = _make_csp(test_db)
    entity = _add_entity(test_db, csp, CLIENT_A[0], uen=CLIENT_A[1])
    entity.verified_hint = None
    test_db.commit()

    assert trusted_cached_uen(
        entity, company_hint=CLIENT_A[0], carrier=MANAGED_ENTITY_CARRIER
    ) is None


def test_unknown_client_reads_not_verified(test_db, acra_stub):
    """No registry match → `verified=False`, and the subject stays the named
    company. Never a substituted identity."""
    csp = _make_csp(test_db)
    entity = _add_entity(test_db, csp, "NOT IN ANY REGISTRY PTE LTD")

    res = asyncio.run(resolve_managed_entity_identity(entity, test_db))

    assert res["verified"] is False
    assert res["uen"] in (None, "")
    assert res["legal_name"] == "NOT IN ANY REGISTRY PTE LTD"


# ── Ownership scoping ─────────────────────────────────────────────────────────


def test_entities_are_scoped_to_their_owner(test_db, acra_stub):
    """Two CSPs may manage the same client company; each owns its own row, and one
    firm's entity list never includes another's."""
    csp_one = _make_csp(test_db)
    csp_two = _make_csp(
        test_db, company="RIVAL CORPORATE SERVICES PTE. LTD.",
        uen="199900003C", site="rival-cs.example",
    )

    _add_entity(test_db, csp_one, CLIENT_A[0], domain=CLIENT_A[2])
    _add_entity(test_db, csp_two, CLIENT_A[0], domain=CLIENT_A[2])

    for owner in (csp_one, csp_two):
        rows = (
            test_db.query(ManagedEntity)
            .filter(ManagedEntity.owner_user_id == owner.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].company_name == CLIENT_A[0]
