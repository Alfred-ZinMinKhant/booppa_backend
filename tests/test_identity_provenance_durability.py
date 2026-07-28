"""The ways a *correct* cached UEN can silently become an untrusted or wrong one.

Both parts of the identity work rest on the same contract: a cached `uen` /
`legal_name` is trustworthy exactly while the `verified_hint` it was resolved from
still matches what the current order names. Three things can break that contract
without any code claiming to touch identity:

1. The nightly ACRA registry refresh bulk-upserts `discovered_vendors`. If the
   upsert's `set_` clause ever grows to "every column", it overwrites provenance
   written by fulfillment, and every previously-verified vendor row becomes
   unprovenanced — i.e. permanently untrusted, so scans stop resolving a UEN they
   had already verified.

2. `IdentityCarrier.own_name_implies_self` short-circuits the trust check when the
   hint equals the row's own name. That is a genuine self-subject signal only on
   `User`, whose `company` is independently-entered account data. On a subject row
   it is a tautology — the row was *looked up by* that name — so enabling it there
   hands out a cached UEN that may have been resolved from a different company
   entirely. This is the hole the managed-entity tests originally surfaced.

3. A raw read of `user.uen` bypasses the gate entirely. The write-path invariant
   catches new *writes*; nothing caught new *reads*, and one was live — see the
   read-side section below.

A fourth property is asserted here too, because it is what makes the cache safe to
keep at all: a company or website the customer names on an order is resolved
against ACRA fresh, and the registry supersedes anything cached.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

from app.core.models import DiscoveredVendor
from app.services import acra_service, evidence_enricher
from app.services.evidence_enricher import (
    MANAGED_ENTITY_CARRIER,
    USER_CARRIER,
    VENDOR_CARRIER,
    IdentityCarrier,
)

# Distinct from every other test's fixtures: `discovered_vendors.uen` is globally
# unique, and the suite shares one database.
REFRESHED_UEN = "200088881R"
OLD_NAME = "NETPOLEONS TECHNOLOGY PTE LTD"
NEW_NAME = "NETPOLEONS TECHNOLOGY PTE. LTD."


# ── 1. The registry refresh must not wipe fulfillment's provenance ────────────


def test_acra_refresh_preserves_verified_provenance(test_db, monkeypatch):
    """A row verified by a scan keeps its `verified_hint` / `verified_at` across a
    registry refresh that rewrites the same row's registry-owned columns."""
    from datetime import datetime

    verified_at = datetime(2026, 1, 2, 3, 4, 5)
    test_db.add(
        DiscoveredVendor(
            id=uuid.uuid4(),
            uen=REFRESHED_UEN,
            company_name=OLD_NAME,
            source="manual",
            verified_hint=OLD_NAME,
            verified_at=verified_at,
        )
    )
    test_db.commit()

    page = {
        "result": {
            "records": [
                {
                    "uen": REFRESHED_UEN,
                    "entity_name": NEW_NAME,
                    "entity_type_desc": "LOCAL COMPANY",
                    "uen_status_desc": "REGISTERED",
                }
            ]
        }
    }
    calls = {"n": 0}

    async def _one_page(client, dataset_id, offset):
        calls["n"] += 1
        return page if calls["n"] == 1 else None

    monkeypatch.setattr(acra_service, "_fetch_page", _one_page)

    assert acra_service.refresh_acra(test_db, dataset_id="stub", max_records=10) == 1

    row = (
        test_db.query(DiscoveredVendor)
        .filter(DiscoveredVendor.uen == REFRESHED_UEN)
        .one()
    )
    # The registry owns the name — that column is expected to move.
    assert row.company_name == NEW_NAME
    # Fulfillment owns the provenance — it must not.
    assert row.verified_hint == OLD_NAME
    assert row.verified_at is not None


def test_refresh_upsert_never_claims_the_identity_columns():
    """Belt-and-braces on the above: the allowlist is the mechanism, so assert the
    mechanism directly rather than only its current effect."""
    import inspect

    src = inspect.getsource(acra_service._refresh_async)
    body = src.split("on_conflict_do_update", 1)[1].split(")", 1)[0]
    # `company_name` is deliberately absent from this list: the registry owns the
    # name, and refreshing it is correct — a hint that no longer matches simply
    # forces a fresh resolve, which is the safe direction.
    for owned_by_fulfillment in ("verified_hint", "verified_at"):
        assert owned_by_fulfillment not in body, (
            f"`{owned_by_fulfillment}` was added to the ACRA upsert's set_ clause. "
            "That column is written by evidence_enricher when a scan verifies a "
            "subject; refreshing it from the registry feed erases the provenance "
            "that makes the cached UEN trustworthy."
        )


# ── 2. The self-subject shortcut belongs to `User` alone ─────────────────────


def test_only_the_user_carrier_may_treat_its_own_name_as_self():
    subject_carriers = {
        "VENDOR_CARRIER": VENDOR_CARRIER,
        "MANAGED_ENTITY_CARRIER": MANAGED_ENTITY_CARRIER,
    }
    for name, carrier in subject_carriers.items():
        assert carrier.own_name_implies_self is False, (
            f"{name}.own_name_implies_self is True. That row is looked up BY its "
            "own name, so the check `hint == row.<own_name_field>` is a tautology "
            "and would trust a cached UEN resolved from a different company."
        )
    assert USER_CARRIER.own_name_implies_self is True


# ── 3. The read side: a raw `user.uen` is as dangerous as a raw write ────────
#
# `test_identity_write_path_invariant` stops new code *writing* an identity around
# the trust gate. Nothing stopped new code *reading* one. That gap was live: the
# PDPA self-declaration worker resolved its company name through
# `display_legal_name` (which re-resolves a cache it does not trust) and then read
# `user.uen` raw, so a corrected name could be paired with the previous subject's
# registration number on the same signed document.
#
# A UEN is never a display string — it is a registry fact about a specific legal
# person. Every read of one must go through `trusted_cached_uen`, which returns
# `None` when the cache cannot be vouched for. The exceptions below are reads that
# are provably about the account's own entity, or are not identity resolution at
# all; each is listed with why.

RAW_UEN_READ = re.compile(r'(?:\buser\.uen\b|getattr\(\s*user\s*,\s*["\']uen["\'])')

ALLOWED_RAW_UEN_READS = {
    # The sanctioned module: this is where the gate itself lives.
    "services/evidence_enricher.py",
    # Existence check only ("has this account been verified before?"), and the value
    # written next goes through `record_verified_identity`.
    "api/stripe_checkout.py",
    # Both reads are of the *vendor's own* row — the public directory entry, and the
    # certificate-verification lookup keyed on `CertificateLog.vendor_id`. The
    # subject is the account by construction.
    "api/government.py",
    # The non-entity branch of the CSP baseline: reached only when the order named
    # neither a managed entity nor a company, i.e. a firm assessing itself.
    "workers/csp_tasks.py",
    # Seeds demo fixtures; never renders a customer document.
    "services/trm_demo_harness.py",
}


def test_uen_is_never_read_around_the_trust_gate():
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in ALLOWED_RAW_UEN_READS or rel == "core/models.py":
            continue
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            line = raw.strip()
            if line.startswith("#") or not RAW_UEN_READ.search(line):
                continue
            offenders.append(f"app/{rel}:{lineno}: {line}")

    assert not offenders, (
        "Raw read of the account's cached UEN. Use "
        "`evidence_enricher.trusted_cached_uen(user, company_hint=..., "
        "website_hint=...)`, which returns None when the cache's provenance does "
        "not match this subject — then render 'Not provided' rather than another "
        "entity's registration number. If the read is provably about the account's "
        "own entity, add the file to ALLOWED_RAW_UEN_READS with the reason.\n\n"
        + "\n".join(offenders)
    )


def test_a_named_subject_is_always_resolved_fresh(test_db, monkeypatch):
    """A company the customer typed on this order is resolved against ACRA now —
    the account's cached UEN is not served in its place, even when that cache
    passes the provenance check, and the registry's answer wins on disagreement."""
    import asyncio as _asyncio

    from app.api.stripe_checkout import _resolve_order_subject
    from app.core.models import User

    stale_uen = "199900009Z"
    fresh_uen = "201234567A"
    company = "NETPOLEONS TECHNOLOGY PTE LTD"

    vendor = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",
        company=company,
        legal_name=company,
        legal_name_hint=company,   # provenance matches, so the cache WOULD be trusted
        uen=stale_uen,
        website="netpoleons.com",
        website_hint="netpoleons.com",
    )
    test_db.add(vendor)
    test_db.commit()

    calls: list[str] = []

    async def _acra(uen=None, company_name=None):
        calls.append(company_name or uen or "")
        return {"found": True, "live": True, "uen": fresh_uen,
                "registered_name": company, "entity_status": "LIVE"}

    monkeypatch.setattr(evidence_enricher, "fetch_acra_status", _acra)

    data = {"company_name": company, "website": "netpoleons.com"}
    _company, _website, is_self = _asyncio.run(
        _resolve_order_subject(
            test_db, vendor, data,
            missing_company_detail="company required",
            gate_acra=False,
        )
    )

    assert is_self is True
    assert calls, "a named subject must trigger a live ACRA lookup, not a cache read"
    assert data["uen"] == fresh_uen, (
        "the stale cached UEN was served for a company the customer named. "
        "Explicit input must be resolved fresh."
    )


def test_every_declared_carrier_is_covered_by_that_rule():
    """A fourth carrier added later inherits the permissive default (True). Fail
    here rather than let it silently reopen the hole."""
    known = {"USER_CARRIER", "VENDOR_CARRIER", "MANAGED_ENTITY_CARRIER"}
    declared = {
        n
        for n, v in vars(evidence_enricher).items()
        if isinstance(v, IdentityCarrier) and n.isupper()
    }
    assert declared == known, (
        f"New identity carrier(s) {sorted(declared - known)}: set "
        "`own_name_implies_self=False` unless the row's name column is "
        "independently-entered account data (only `User` qualifies), then add it "
        "to this test."
    )
