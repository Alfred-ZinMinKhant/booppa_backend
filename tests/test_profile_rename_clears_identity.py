"""Renaming an account must invalidate its cached identity, on every rename path.

The trust gate deliberately does NOT catch this one. `_cache_trusted_for_hint` treats
a `company_hint` equal to the account's own `company` as a self-render and returns
True (`own_name_implies_self`) — correct for a `User`, since an account rendering
under its own name usually is the subject. But it means a stale `uen` surviving a
rename is vouched for by the NEW name: the account says "I am NEWCO", the cache says
"UEN 21374700J, confirmed for OLDCO", and the gate waves it through because the hint
matches `user.company`.

Fed to the registry that UEN returns a real, correct ACRA record — for a company this
account no longer is. So the invariant has to be enforced at the write: every endpoint
that renames an account clears the cache and re-resolves. `PATCH /vendor/profile` did
not, which made it a live route into the SPQR shape.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.models import User


@pytest.fixture
def no_registry(monkeypatch):
    """The re-resolve after a rename must not reach the network in tests. Returning
    no match is also the honest worst case: the account ends up unresolved rather
    than carrying the previous company's identity."""
    async def _fake(*a, **kw):
        return {"found": False}

    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _fake)


@pytest.fixture
def vendor(test_db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",
        is_active=True,
        company="SPQR COMMUNICATIONS PTE. LTD.",
        website="https://spqr.example",
        legal_name="SPQR COMMUNICATIONS PTE. LTD.",
        legal_name_hint="SPQR COMMUNICATIONS PTE. LTD.",
        website_hint="https://spqr.example",
        uen="21374700J",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)
    return user


def _auth(email: str) -> dict:
    from app.core.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


def test_rename_via_vendor_profile_drops_the_old_identity(
    client, test_db, vendor, no_registry
):
    """The load-bearing one. `PATCH /vendor/profile` used to write `company` and
    nothing else, leaving a UEN belonging to the previous company attached to an
    account that now claims a different name."""
    resp = client.patch(
        "/api/vendor/profile",
        json={"company": "NEWCO ADVISORY PTE. LTD."},
        headers=_auth(vendor.email),
    )
    assert resp.status_code == 200, resp.text

    test_db.refresh(vendor)
    assert vendor.company == "NEWCO ADVISORY PTE. LTD."
    assert vendor.uen != "21374700J", (
        "the previous company's UEN survived the rename — the account's new name "
        "now vouches for it, and the registry will confirm it as a real entity"
    )
    assert vendor.legal_name != "SPQR COMMUNICATIONS PTE. LTD."


def test_rename_does_not_leave_provenance_behind(client, test_db, vendor, no_registry):
    """Clearing the value but keeping `legal_name_hint` would be worse than leaving
    both: the row would look like it has trustworthy provenance for a value that no
    longer exists."""
    client.patch(
        "/api/vendor/profile",
        json={"company": "NEWCO ADVISORY PTE. LTD."},
        headers=_auth(vendor.email),
    )

    test_db.refresh(vendor)
    assert vendor.legal_name_hint != "SPQR COMMUNICATIONS PTE. LTD."


def test_updating_industry_alone_keeps_the_identity(client, test_db, vendor, no_registry):
    """Not a rename. Clearing a verified identity here would cost the account its
    resolved UEN for an unrelated edit."""
    resp = client.patch(
        "/api/vendor/profile",
        json={"industry": "Technology"},
        headers=_auth(vendor.email),
    )
    assert resp.status_code == 200, resp.text

    test_db.refresh(vendor)
    assert vendor.uen == "21374700J"
    assert vendor.industry == "Technology"


def test_resubmitting_the_same_company_is_not_a_rename(
    client, test_db, vendor, no_registry
):
    """A form that PATCHes every field on save must not destroy identity each time."""
    client.patch(
        "/api/vendor/profile",
        json={"company": "SPQR COMMUNICATIONS PTE. LTD.", "industry": "Media"},
        headers=_auth(vendor.email),
    )

    test_db.refresh(vendor)
    assert vendor.uen == "21374700J"
