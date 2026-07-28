"""The two ways a *correct* cached UEN can silently become an untrusted or wrong one.

Both parts of the identity work rest on the same contract: a cached `uen` /
`legal_name` is trustworthy exactly while the `verified_hint` it was resolved from
still matches what the current order names. Two things can break that contract
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
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.models import DiscoveredVendor
from app.services import acra_service, evidence_enricher
from app.services.evidence_enricher import (
    MANAGED_ENTITY_CARRIER,
    USER_CARRIER,
    VENDOR_CARRIER,
    IdentityCarrier,
)

REFRESHED_UEN = "201234567A"
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
