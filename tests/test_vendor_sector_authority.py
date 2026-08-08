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


# ── Every identity reader, not just the deliverable ──────────────────────────
#
# The original fix converted the write paths and the Competitor Signals PDF, but
# eleven other call sites still did an unordered `.first()`. Each one could hand
# a different answer for the same vendor in the same request cycle. These pin the
# readers that feed something a customer or a procurement officer actually sees.


def _ambiguous_vendor(db):
    """A vendor with two undated sector rows and no `User.industry` anchor.

    This is precisely the state `resolve_primary_sector` refuses to guess at, and
    the state every `.first()` reader used to resolve arbitrarily.
    """
    user = make_user(db, plan="vendor_pro")
    user.industry = None
    db.add(VendorSector(vendor_id=user.id, sector="Construction"))
    db.add(VendorSector(vendor_id=user.id, sector="Fintech"))
    db.commit()
    db.execute(
        text("UPDATE vendor_sectors SET created_at = NULL WHERE vendor_id = :v"),
        {"v": str(user.id)},
    )
    db.commit()
    return user


def test_tender_alert_digest_skips_rather_than_defaulting_to_it(test_db):
    """The digest loop defaulted an unresolvable vendor to sector 'IT'.

    That produced a complete, plausible IT tender shortlist for a vendor of
    unknown sector — the exact failure mode as the Construction report, and one
    a vendor would act on by bidding.
    """
    import inspect
    from app.workers import tasks

    src = inspect.getsource(tasks.send_tender_alerts)
    assert 'else "IT"' not in src, "the 'IT' fallback is back"
    assert "resolve_primary_sector" in src


def test_government_portal_does_not_label_unresolvable_vendor_general(test_db):
    """`_vendor_row` defaulted to 'General', which is itself a real sector label
    (Miscellaneous maps to it) — so an unresolvable vendor was indistinguishable
    from one that genuinely trades there."""
    from app.api.government import _vendor_row

    user = _ambiguous_vendor(test_db)
    row = _vendor_row(user, None, None, resolve_primary_sector(test_db, user.id))
    assert row["industry"] is None


def test_government_vendor_list_does_not_duplicate_multi_sector_vendors(test_db):
    """VendorSector is one-to-many; the old outer join emitted one result row per
    sector, so a two-sector vendor appeared twice and was counted twice."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import create_access_token, get_password_hash
    from app.core.models import User

    user = _ambiguous_vendor(test_db)
    user.role = "VENDOR"
    user.is_active = True

    # The government endpoints are authenticated now (P0-1) — this assertion is
    # about the sector join, so it needs a real GOVERNMENT caller rather than an
    # anonymous one.
    officer_email = "officer@agency.gov.sg"
    test_db.add(
        User(
            email=officer_email,
            hashed_password=get_password_hash("Passw0rd!123"),
            company="Test Agency",
            role="GOVERNMENT",
        )
    )
    test_db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(data={'sub': officer_email})}"}

    with TestClient(app) as client:
        resp = client.get(
            "/api/government/vendors", params={"per_page": 100}, headers=headers
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ids = [v["id"] for v in body["vendors"]]
    assert ids.count(str(user.id)) == 1, "vendor duplicated by the sector join"


def test_score_snapshot_does_not_persist_an_arbitrary_sector(test_db):
    """scoring.py wrote the `.first()` sector into permanent score history."""
    import inspect
    from app.services import scoring

    src = inspect.getsource(scoring._record_score_snapshot_lazy)
    assert "resolve_primary_sector" in src
    assert "query(VendorSector)" not in src


def test_no_identity_reader_still_uses_unordered_first(test_db):
    """Guard against a new `.first()` identity read being reintroduced.

    Only `sync_vendor_sector`'s existence check may do this — it asks *whether a
    row exists*, not *which sector the vendor is*.
    """
    import pathlib
    import re

    root = pathlib.Path("app")
    offenders = []
    for path in root.rglob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"query\(VendorSector\).*first\(\)", line):
                if path.name == "tender_service.py":
                    continue  # the existence check in sync_vendor_sector
                offenders.append(f"{path}:{i}")
    assert not offenders, f"unordered VendorSector identity reads: {offenders}"
