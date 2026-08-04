"""Authoritative writes + deterministic reads for `vendor_sectors`.

The reported bug: matchmove, a MAS-licensed payments company (MPI PS20200239),
was issued a Competitor Awareness Signals report headed CONSTRUCTION.

Root cause was not the TRM alias map and not an ACRA/SSIC lookup (no SSIC lookup
exists — the open ACRA dataset carries no such field). It was two things
compounding: `vendor_sectors` was append-only, so a stale row outlived every
later correction; and every reader did an unordered `.first()`, so which stale
row won was left to Postgres.

It failed silently because a wrong sector label lowercases into a *valid*
`GebizAwardHistory.sector` key — the report fills with a complete, plausible
competitor set for the wrong industry rather than rendering empty.

These tests pin the three properties that stop it recurring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.models import VendorSector
from app.services.tender_service import (
    resolve_primary_sector,
    set_vendor_sector,
    sync_vendor_sector,
)
from tests._test_helpers import make_user


def _sectors(db, user):
    return sorted(
        (r.sector or "") for r in
        db.query(VendorSector).filter(VendorSector.vendor_id == user.id).all()
    )


# ── Writes ───────────────────────────────────────────────────────────────────

def test_set_replaces_a_stale_row_instead_of_accumulating(test_db):
    """The matchmove shape: a wrong sector written once must not survive."""
    user = make_user(test_db, plan="vendor_pro")
    test_db.add(VendorSector(vendor_id=user.id, sector="Construction"))
    test_db.commit()

    set_vendor_sector(test_db, user.id, "Fintech")

    assert _sectors(test_db, user) == ["Fintech"]


def test_set_is_idempotent(test_db):
    user = make_user(test_db, plan="vendor_pro")
    set_vendor_sector(test_db, user.id, "Fintech")
    set_vendor_sector(test_db, user.id, "Fintech")

    assert _sectors(test_db, user) == ["Fintech"]


def test_set_normalises_gebiz_categories(test_db):
    """'Works' maps to Construction — one of the routes the bad row arrived by."""
    user = make_user(test_db, plan="vendor_pro")
    assert set_vendor_sector(test_db, user.id, "Works") == "Construction"
    assert _sectors(test_db, user) == ["Construction"]


def test_sync_still_accumulates_for_genuinely_multi_sector_use(test_db):
    """`sync_vendor_sector` keeps its additive behaviour — tender shortlisting
    legitimately spans categories. Only identity writes switched."""
    user = make_user(test_db, plan="vendor_pro")
    sync_vendor_sector(test_db, user.id, "IT")
    sync_vendor_sector(test_db, user.id, "Security")

    assert _sectors(test_db, user) == ["IT", "Security"]


# ── Reads ────────────────────────────────────────────────────────────────────

def test_resolve_returns_none_when_no_sector_is_set(test_db):
    """No default. A fallback sector produces a complete and wrong report."""
    user = make_user(test_db, plan="vendor_pro")
    assert resolve_primary_sector(test_db, user.id) is None


def test_resolve_prefers_the_row_matching_the_account_industry(test_db):
    user = make_user(test_db, plan="vendor_pro")
    user.industry = "Fintech"
    test_db.add(VendorSector(vendor_id=user.id, sector="Construction"))
    test_db.add(VendorSector(vendor_id=user.id, sector="Fintech"))
    test_db.commit()

    assert resolve_primary_sector(test_db, user.id) == "Fintech"


def test_resolve_breaks_ties_on_recency_when_industry_does_not_match(test_db):
    user = make_user(test_db, plan="vendor_pro")
    now = datetime.now(timezone.utc)
    test_db.add(VendorSector(
        vendor_id=user.id, sector="Construction", created_at=now - timedelta(days=400),
    ))
    test_db.add(VendorSector(vendor_id=user.id, sector="Fintech", created_at=now))
    test_db.commit()

    assert resolve_primary_sector(test_db, user.id) == "Fintech"


def test_resolve_refuses_to_guess_between_undated_conflicting_rows(test_db):
    """Rows predating `created_at` have no knowable order.

    Returning either one would be the original bug with extra steps: the caller
    renders a full report under a sector nobody actually established. None forces
    'not assessed'.
    """
    user = make_user(test_db, plan="vendor_pro")
    test_db.add(VendorSector(vendor_id=user.id, sector="Construction"))
    test_db.add(VendorSector(vendor_id=user.id, sector="Fintech"))
    test_db.commit()
    # The column carries a Python-side `default=utcnow`, which fires even when
    # the attribute is explicitly None — so a legacy NULL cannot be reproduced
    # through the ORM. Blank it in SQL, which is the state the migration leaves
    # pre-existing rows in.
    test_db.execute(
        text("UPDATE vendor_sectors SET created_at = NULL WHERE vendor_id = :v"),
        {"v": str(user.id)},
    )
    test_db.commit()

    assert resolve_primary_sector(test_db, user.id) is None


# ── The deliverable ──────────────────────────────────────────────────────────

def test_competitor_signals_does_not_default_to_it(test_db):
    """`_sector_for_vendor` used to return 'IT' for an unresolvable vendor."""
    from app.services.competitor_signals_generator import _sector_for_vendor

    user = make_user(test_db, plan="vendor_pro")
    assert _sector_for_vendor(test_db, user.id) is None


def test_competitor_signals_reports_the_corrected_sector(test_db):
    from app.services.competitor_signals_generator import _sector_for_vendor

    user = make_user(test_db, plan="vendor_pro")
    user.industry = "Fintech"
    test_db.add(VendorSector(vendor_id=user.id, sector="Construction"))
    test_db.commit()

    # Before reconciliation the identity anchor already wins the read...
    set_vendor_sector(test_db, user.id, "Fintech")
    # ...and after it, the stale row is gone entirely.
    assert _sectors(test_db, user) == ["Fintech"]
    assert _sector_for_vendor(test_db, user.id) == "FINTECH"
