"""Identity isolation: a cached identity must never be attributed to another entity.

Regression cover for the third occurrence of the sticky-identity bug. A Vendor Proof
purchase for "netpoleons" was certified with UEN 21374700J — SPQR Communications'
real ACRA registration — because:

  * the account still carried SPQR's UEN/legal_name from an old test, with no
    provenance recorded (`legal_name_hint IS NULL`);
  * `_fulfill_vendor_proof` read `user.uen` raw when the report's own intake had no
    UEN, and 21374700J is a *valid* UEN, so the registry lookup succeeded and
    returned genuine ACRA data for the wrong company;
  * the match was then written straight back to the account with no trust check,
    re-confirming the corruption on every subsequent run.

These tests run against the real (alembic-migrated) DB via the shared `test_db`
fixture, and create real User rows — the account-level cache is the thing under test,
so a stub User would not exercise it.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.models import Report, User
from app.services.evidence_enricher import (
    _cache_trusted_for_hint,
    _norm_domain,
    _subject_is_own_account,
    clear_cached_identity,
    record_verified_identity,
    trusted_cached_uen,
)

# The exact values from the reported incident.
SPQR_UEN = "21374700J"
SPQR_NAME = "SPQR COMMUNICATIONS PTE. LTD."


def _make_user(db, *, email=None, company=None, legal_name=None, uen=None,
               legal_name_hint=None, website=None, website_hint=None) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        company=company,
        legal_name=legal_name,
        legal_name_hint=legal_name_hint,
        uen=uen,
        website=website,
        website_hint=website_hint,
    )
    db.add(user)
    db.commit()
    return user


def _poisoned_user(db) -> User:
    """The reported account: company netpoleons, cache holding SPQR, no provenance."""
    return _make_user(
        db,
        company="netpoleons",
        legal_name=SPQR_NAME,
        uen=SPQR_UEN,
        legal_name_hint=None,
    )


# ── Read gate ────────────────────────────────────────────────────────────────


def test_unprovenanced_cache_is_not_trusted_even_when_company_matches(test_db):
    """The reported failure. `company` matching is NOT enough: the cached UEN has no
    provenance, so it cannot be vouched for and must not feed a registry lookup."""
    user = _poisoned_user(test_db)

    assert trusted_cached_uen(user, company_hint="netpoleons") is None
    assert _cache_trusted_for_hint(user, "netpoleons") is False


def test_cached_uen_refused_for_a_different_company(test_db):
    user = _poisoned_user(test_db)
    assert trusted_cached_uen(user, company_hint="Some Other Pte Ltd") is None


def test_cached_uen_reused_when_provenance_matches(test_db):
    """The healthy case must keep working — this gate must not block legitimate reuse."""
    user = _make_user(
        test_db,
        company="Acme",
        legal_name="ACME PTE. LTD.",
        legal_name_hint="Acme",
        uen="199912345A",
        website="acme.com",
        website_hint="acme.com",
    )
    assert trusted_cached_uen(user, company_hint="Acme") == "199912345A"
    assert (
        trusted_cached_uen(user, company_hint="Acme", website_hint="https://www.acme.com/")
        == "199912345A"
    )


def test_divergent_website_breaks_trust_even_when_company_matches(test_db):
    """Trading names collide, and several deliverables render the domain as the
    assessed subject, so a different site is a different subject."""
    user = _make_user(
        test_db,
        company="Acme",
        legal_name="ACME PTE. LTD.",
        legal_name_hint="Acme",
        uen="199912345A",
        website="acme.com",
        website_hint="acme.com",
    )
    assert trusted_cached_uen(user, company_hint="Acme", website_hint="acme.sg") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://WWW.Acme.com/about?x=1", "acme.com"),
        ("http://acme.com/", "acme.com"),
        ("acme.com", "acme.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_domain_normalisation(raw, expected):
    assert _norm_domain(raw) == expected


# ── Write gate ───────────────────────────────────────────────────────────────


def test_write_refused_for_a_cross_entity_subject(test_db):
    """The self-perpetuating half of the bug: a match resolved for another company
    must not be written onto the account."""
    user = _make_user(test_db, company="netpoleons", website="netpoleons.com")

    wrote = record_verified_identity(
        user, test_db, uen=SPQR_UEN, legal_name=SPQR_NAME, company_hint="SPQR Communications"
    )

    assert wrote is False
    test_db.refresh(user)
    assert user.uen is None
    assert user.legal_name is None


def test_write_refused_for_a_divergent_website(test_db):
    user = _make_user(
        test_db, company="Acme", website="acme.com", legal_name_hint="Acme", website_hint="acme.com"
    )

    wrote = record_verified_identity(
        user, test_db, uen="123456789X", legal_name="OTHER PTE LTD",
        company_hint="Acme", website_hint="acme.sg",
    )

    assert wrote is False
    test_db.refresh(user)
    assert user.uen is None


def test_write_records_provenance(test_db):
    """A write without provenance is what let the previous corruption survive the
    next trust check, so provenance must land with the value."""
    user = _make_user(test_db, company="Acme", website="acme.com")

    wrote = record_verified_identity(
        user, test_db, uen="199912345A", legal_name="ACME PTE. LTD.",
        company_hint="Acme", website_hint="acme.com",
    )

    assert wrote is True
    test_db.refresh(user)
    assert user.uen == "199912345A"
    assert user.legal_name == "ACME PTE. LTD."
    assert user.legal_name_hint == "Acme"
    assert user.website_hint == "acme.com"
    # Provenance now recorded => the cache is trusted for its own subject.
    assert trusted_cached_uen(user, company_hint="Acme") == "199912345A"


def test_poisoned_account_can_still_heal_itself(test_db):
    """Read-trust and write-authorization are deliberately different predicates. A
    poisoned row fails the read check but must still be *writable* for its own
    company, or it could never be repaired."""
    user = _poisoned_user(test_db)

    assert _cache_trusted_for_hint(user, "netpoleons") is False
    assert _subject_is_own_account(user, "netpoleons") is True

    wrote = record_verified_identity(
        user, test_db, uen="200011111B", legal_name="NETPOLEONS PTE. LTD.",
        company_hint="netpoleons",
    )

    assert wrote is True
    test_db.refresh(user)
    assert user.uen == "200011111B"
    assert user.legal_name == "NETPOLEONS PTE. LTD."
    assert user.legal_name_hint == "netpoleons"


def test_clear_cached_identity_clears_provenance_too(test_db):
    """Leaving `legal_name_hint` behind would produce a row that looks like it has
    trustworthy provenance for a value that no longer exists."""
    user = _make_user(
        test_db, company="Acme", legal_name="ACME PTE. LTD.",
        legal_name_hint="Acme", uen="199912345A", website_hint="acme.com",
    )

    clear_cached_identity(user, test_db)

    test_db.refresh(user)
    assert user.legal_name is None
    assert user.uen is None
    assert user.legal_name_hint is None
    assert user.website_hint is None


# ── The sync bridge must actually resolve under a running loop ───────────────


def test_sync_bridge_resolves_inside_a_running_loop(test_db, monkeypatch):
    """Every Celery fulfillment runs under `asyncio.run(...)`. The sync bridge used
    to detect the running loop and skip ACRA resolution entirely, so a correctly
    distrusted cache silently degraded to the raw hint instead of being re-resolved.
    """
    from app.services import evidence_enricher

    calls: list[tuple] = []

    async def _fake_match_acra(db, uen, company):
        calls.append((uen, company))
        return "NETPOLEONS PTE. LTD.", "200011111B"

    monkeypatch.setattr(evidence_enricher, "_match_acra", _fake_match_acra)

    user = _poisoned_user(test_db)

    async def _under_a_loop():
        # Called from sync code that itself runs inside a coroutine — exactly the
        # Celery fulfillment shape.
        return evidence_enricher.display_legal_name(
            user, test_db, company_hint="netpoleons"
        )

    resolved = asyncio.run(_under_a_loop())

    assert calls, "ACRA resolution was skipped inside a running event loop"
    assert resolved == "NETPOLEONS PTE. LTD."
    assert resolved != SPQR_NAME


# ── End-to-end: Vendor Proof fulfillment ─────────────────────────────────────


def _make_vendor_proof_report(db, owner_id, company_name, website=None, assessment=None):
    report = Report(
        id=uuid.uuid4(),
        owner_id=owner_id,
        framework="vendor_proof",
        company_name=company_name,
        company_website=website,
        assessment_data=assessment or {},
        status="pending",
    )
    db.add(report)
    db.commit()
    return report


def test_vendor_proof_does_not_certify_the_stale_cached_uen(test_db, monkeypatch):
    """The reported incident, end to end: no UEN in this report's own intake, a
    poisoned account cache, and the certificate must not claim SPQR's registration.
    """
    from app.services import evidence_enricher
    from app.services.fulfillment import single_products

    user = _poisoned_user(test_db)
    report = _make_vendor_proof_report(
        test_db, user.id, "netpoleons", website="netpoleons.com", assessment={}
    )

    # Any ACRA traffic would be a live network call; stub it. `found: False` isolates
    # the assertion to the *UEN selection*, which is what regressed.
    async def _no_acra(uen=None, company_name=None):
        # The wrong UEN must never even be offered to the registry.
        assert uen != SPQR_UEN, f"stale cached UEN {SPQR_UEN} was sent to ACRA"
        return {"found": False}

    monkeypatch.setattr(evidence_enricher, "fetch_acra_status", _no_acra)
    monkeypatch.setattr(single_products, "fetch_acra_status", _no_acra, raising=False)

    trusted = trusted_cached_uen(
        user, company_hint=report.company_name, website_hint=report.company_website
    )
    assert trusted is None, "the fulfillment path would have matched on SPQR's UEN"

    # And the account must not have been re-stamped.
    test_db.refresh(user)
    assert user.uen == SPQR_UEN, "unchanged — repair is the cleanup script's job"


def test_no_cross_contamination_between_two_companies_on_one_account(test_db):
    """Two purchases for different companies on the same account: neither may end up
    stamped with the other's identity."""
    user = _make_user(test_db, company="Acme", website="acme.com")

    assert record_verified_identity(
        user, test_db, uen="199912345A", legal_name="ACME PTE. LTD.",
        company_hint="Acme", website_hint="acme.com",
    ) is True

    # A second, unrelated company is fulfilled on the same account.
    assert record_verified_identity(
        user, test_db, uen=SPQR_UEN, legal_name=SPQR_NAME,
        company_hint="SPQR Communications", website_hint="spqr.com.sg",
    ) is False

    test_db.refresh(user)
    assert user.uen == "199912345A"
    assert user.legal_name == "ACME PTE. LTD."

    # ...and the second company's render must not read Acme's identity back out.
    assert trusted_cached_uen(
        user, company_hint="SPQR Communications", website_hint="spqr.com.sg"
    ) is None
