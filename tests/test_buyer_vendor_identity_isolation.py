"""Buyer→Vendor identity isolation: one login, many subjects.

`tests/test_identity_isolation.py` covers the account-level cache — the case where
one account resolves two different companies over time. This file covers the
structurally different case the account-level fix cannot reach:

    A BUYER scans many vendors from one login.

For a buyer there is no sense in which the account's own company is the report's
subject, so the old `company_name = req_company or user.company` fallback could only
ever produce a wrong answer: either the buyer's own identity stamped on a vendor's
report (the SPQR leak), or vendor A's identity carried into vendor B's report. The
fix moves the subject onto the `DiscoveredVendor` row via `VENDOR_CARRIER`, so these
tests assert on that row and on the resolved result, never on the buyer's `User`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.models import DiscoveredVendor, User
from app.services.evidence_enricher import (
    VENDOR_CARRIER,
    resolve_subject_identity,
    trusted_cached_uen,
)

BUYER_COMPANY = "ACME PROCUREMENT PTE. LTD."
BUYER_UEN = "199900001A"

VENDOR_A = ("NETPOLEONS TECHNOLOGY PTE LTD", "201234567A", "netpoleons.com")
VENDOR_B = ("SPQR COMMUNICATIONS PTE. LTD.", "21374700J", "spqr.example")


def _make_buyer(db) -> User:
    """A procurement account with a fully-populated identity of its own — the thing
    that must never end up on a vendor's report."""
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="PROCUREMENT",
        company=BUYER_COMPANY,
        legal_name=BUYER_COMPANY,
        legal_name_hint=BUYER_COMPANY,
        uen=BUYER_UEN,
        website="acme-procurement.example",
        website_hint="acme-procurement.example",
    )
    db.add(user)
    db.commit()
    return user


def _seed_vendor(db, name, uen, domain) -> DiscoveredVendor:
    row = DiscoveredVendor(
        id=uuid.uuid4(),
        company_name=name,
        uen=uen,
        domain=domain,
        website=f"https://{domain}",
        country="Singapore",
        source="acra",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if resolution reaches for the live registry. These subjects are
    all present locally; a network call would mean the DiscoveredVendor name/domain
    pre-step regressed."""
    from app.services import evidence_enricher

    async def _boom(*a, **kw):
        raise AssertionError("live ACRA lookup should not be needed for a seeded vendor")

    monkeypatch.setattr(evidence_enricher, "fetch_acra_status", _boom)


# ── The core isolation property ───────────────────────────────────────────────


def test_two_vendor_scans_from_one_buyer_stay_separate(test_db, no_network):
    buyer = _make_buyer(test_db)
    _seed_vendor(test_db, *VENDOR_A)
    _seed_vendor(test_db, *VENDOR_B)

    a = asyncio.run(resolve_subject_identity(test_db, VENDOR_A[0], website=VENDOR_A[2]))
    b = asyncio.run(resolve_subject_identity(test_db, VENDOR_B[0], website=VENDOR_B[2]))

    assert a["uen"] == VENDOR_A[1]
    assert b["uen"] == VENDOR_B[1]
    assert a["legal_name"] == VENDOR_A[0]
    assert b["legal_name"] == VENDOR_B[0]

    # Neither report ever borrows the buyer's identity...
    for res in (a, b):
        assert res["uen"] != BUYER_UEN
        assert res["legal_name"] != BUYER_COMPANY

    # ...and the buyer's account is untouched by either scan.
    test_db.refresh(buyer)
    assert buyer.uen == BUYER_UEN
    assert buyer.company == BUYER_COMPANY
    assert buyer.legal_name == BUYER_COMPANY
    assert buyer.legal_name_hint == BUYER_COMPANY


def test_order_of_scans_does_not_leak_the_first_subject(test_db, no_network):
    """The failure mode being guarded: scan A, then scan B, and B inherits A."""
    _make_buyer(test_db)
    _seed_vendor(test_db, *VENDOR_A)
    _seed_vendor(test_db, *VENDOR_B)

    asyncio.run(resolve_subject_identity(test_db, VENDOR_A[0], website=VENDOR_A[2]))
    second = asyncio.run(resolve_subject_identity(test_db, VENDOR_B[0], website=VENDOR_B[2]))

    assert second["uen"] == VENDOR_B[1]
    assert second["uen"] != VENDOR_A[1]


# ── Write-back lands on the vendor row, with provenance ───────────────────────


def test_verification_is_recorded_on_the_vendor_row(test_db, no_network):
    _make_buyer(test_db)
    row = _seed_vendor(test_db, *VENDOR_A)
    assert row.verified_hint is None

    asyncio.run(resolve_subject_identity(test_db, VENDOR_A[0], website=VENDOR_A[2]))

    test_db.refresh(row)
    assert row.verified_hint == VENDOR_A[0]
    assert row.verified_at is not None

    # And that provenance is what makes the cached UEN reusable next time.
    assert (
        trusted_cached_uen(row, company_hint=VENDOR_A[0], carrier=VENDOR_CARRIER)
        == VENDOR_A[1]
    )


def test_vendor_cache_is_not_trusted_for_a_different_subject(test_db, no_network):
    """Same provenance contract as the account rows: a cached UEN is only reusable
    for the company it was resolved FROM."""
    row = _seed_vendor(test_db, *VENDOR_A)
    asyncio.run(resolve_subject_identity(test_db, VENDOR_A[0], website=VENDOR_A[2]))
    test_db.refresh(row)

    assert (
        trusted_cached_uen(row, company_hint=VENDOR_B[0], carrier=VENDOR_CARRIER) is None
    )


def test_unprovenanced_vendor_row_is_not_trusted(test_db):
    """A scraped / `source='manual'` row is not trustworthy just because it exists —
    NULL `verified_hint` means unknown provenance, which is never trusted."""
    row = _seed_vendor(test_db, *VENDOR_A)
    assert row.verified_hint is None
    assert (
        trusted_cached_uen(row, company_hint=VENDOR_A[0], carrier=VENDOR_CARRIER) is None
    )


# ── No match must mean "not verified", never a guess ──────────────────────────


def test_unknown_vendor_resolves_to_not_verified(test_db, monkeypatch):
    from app.services import evidence_enricher

    async def _not_found(*a, **kw):
        return {"found": False}

    monkeypatch.setattr(evidence_enricher, "fetch_acra_status", _not_found)

    buyer = _make_buyer(test_db)
    res = asyncio.run(resolve_subject_identity(test_db, "TOTALLY UNKNOWN VENDOR LLP"))

    assert res["verified"] is False
    assert res["uen"] is None
    # Falls back to the named subject, never to the buyer's identity.
    assert res["legal_name"] == "TOTALLY UNKNOWN VENDOR LLP"
    test_db.refresh(buyer)
    assert buyer.uen == BUYER_UEN


# ── Checkout never substitutes the account for an unnamed subject ─────────────


def test_buyer_role_account_may_not_self_subject(test_db):
    from app.api.stripe_checkout import _account_may_self_subject

    buyer = _make_buyer(test_db)
    assert _account_may_self_subject(buyer) is False


def test_checkout_422s_instead_of_falling_back_to_the_buyer(test_db):
    """The whole point: an order that names no company is an error, not an
    invitation to use whoever is logged in."""
    from fastapi import HTTPException

    from app.api.stripe_checkout import _resolve_order_subject

    buyer = _make_buyer(test_db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _resolve_order_subject(
                test_db, buyer, {}, missing_company_detail="company required",
            )
        )
    assert exc.value.status_code == 422


def test_checkout_does_not_stamp_the_vendor_name_onto_the_buyer(test_db, no_network):
    from app.api.stripe_checkout import _resolve_order_subject

    buyer = _make_buyer(test_db)
    _seed_vendor(test_db, *VENDOR_A)

    data = {"company_name": VENDOR_A[0], "website": VENDOR_A[2]}
    company, website, is_self = asyncio.run(
        _resolve_order_subject(
            test_db, buyer, data, missing_company_detail="company required",
        )
    )

    assert company == VENDOR_A[0]
    assert is_self is False
    assert data["uen"] == VENDOR_A[1]

    test_db.refresh(buyer)
    assert buyer.company == BUYER_COMPANY
    assert buyer.uen == BUYER_UEN
    assert buyer.website == "acme-procurement.example"
