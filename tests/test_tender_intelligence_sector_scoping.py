"""Unit tests for Tender Intelligence sector-scoping and sync_vendor_sector logic."""
from unittest.mock import MagicMock
from app.services.tender_service import sync_vendor_sector, _CATEGORY_TO_SECTOR


def test_sync_vendor_sector_normalises_and_creates_row():
    """`sync_vendor_sector` maps raw industry strings via `_CATEGORY_TO_SECTOR`."""
    db = MagicMock()
    # Mock existing query to return None (no row exists yet)
    db.query.return_value.filter.return_value.first.return_value = None

    # Test raw industry string matching known category
    resolved = sync_vendor_sector(db, "test-user-id", "IT & Telecommunication")
    assert resolved == "IT"
    db.add.assert_called_once()
    assert db.add.call_args[0][0].sector == "IT"

    # Test custom industry string
    db.reset_mock()
    db.query.return_value.filter.return_value.first.return_value = None
    resolved2 = sync_vendor_sector(db, "test-user-id", "Cybersecurity Analytics")
    assert resolved2 == "Cybersecurity Analytics"
    db.add.assert_called_once()
    assert db.add.call_args[0][0].sector == "Cybersecurity Analytics"


def test_sync_vendor_sector_idempotent_when_already_exists():
    """`sync_vendor_sector` does not insert a duplicate row if one exists."""
    db = MagicMock()
    existing_row = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing_row

    resolved = sync_vendor_sector(db, "test-user-id", "Professional Services")
    assert resolved == "Professional Services"
    db.add.assert_not_called()


def test_sync_vendor_sector_handles_none_and_empty():
    """`sync_vendor_sector` degrades gracefully on None or empty strings."""
    db = MagicMock()
    assert sync_vendor_sector(db, None, "IT") is None
    assert sync_vendor_sector(db, "user-id", "") is None
    assert sync_vendor_sector(db, "user-id", None) is None
    db.add.assert_not_called()


def test_primary_sector_falls_back_to_user_industry():
    """In-app sector must match the digest: fall back to User.industry, not 'IT'.

    A standalone Tender Intelligence subscriber has no VendorSector row until
    something seeds one. The digest already falls back to User.industry; the
    in-app view must agree or the customer sees two different sectors.
    """
    from unittest.mock import patch
    from app.api.tender_intelligence import _primary_sector

    db = MagicMock()
    # No VendorSector rows for this vendor...
    db.query.return_value.filter.return_value.all.return_value = []
    # ...but the account has an industry on file.
    user = MagicMock()
    user.industry = "IT & Telecommunication"
    db.query.return_value.filter.return_value.first.return_value = user

    # The write-back is authoritative (`set_vendor_sector`, which replaces) —
    # the industry is an identity fact, not one sector among several.
    with patch("app.services.tender_service.set_vendor_sector", return_value="IT") as _set:
        assert _primary_sector(db, "vendor-id") == "IT"
        _set.assert_called_once()


def test_primary_sector_defaults_only_when_nothing_on_file():
    """With no sector and no industry, the 'IT' default is the honest fallback."""
    from app.api.tender_intelligence import _primary_sector

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    user = MagicMock()
    user.industry = None
    db.query.return_value.filter.return_value.first.return_value = user

    assert _primary_sector(db, "vendor-id") == "IT"


# ── CHECKOUT SECTOR GATE ──────────────────────────────────────────────────────
# A standalone Tender Intelligence subscriber has no Vendor Active/Pro purchase
# to seed VendorSector from an ACRA lookup, so the sector has to be captured at
# checkout or the digest is "all sectors" permanently.

def test_checkout_gate_422s_when_no_sector_and_no_industry():
    """The unresolvable case must prompt, not silently sell a broad digest."""
    import pytest
    from fastapi import HTTPException
    from app.api.stripe_checkout import _resolve_tender_sector

    user = MagicMock()
    user.industry = None
    data = {}
    with pytest.raises(HTTPException) as exc:
        _resolve_tender_sector(user, data)
    assert exc.value.status_code == 422
    # The frontend routes this prompt by matching /sector/i, and must not fall
    # into the earlier /website/i or /company/i branches.
    assert "sector" in exc.value.detail.lower()
    assert "website" not in exc.value.detail.lower()
    assert "company" not in exc.value.detail.lower()


def test_checkout_gate_falls_back_to_profile_industry():
    """Industry already on file is enough — don't prompt a returning account."""
    from app.api.stripe_checkout import _resolve_tender_sector

    user = MagicMock()
    user.industry = "Construction"
    data = {}
    assert _resolve_tender_sector(user, data) == "Construction"
    # Must reach Stripe metadata so subscription activation can seed VendorSector.
    assert data["sector"] == "Construction"


def test_checkout_gate_prefers_order_sector_over_stale_profile():
    """What the buyer just typed beats an industry that predates the order."""
    from app.api.stripe_checkout import _resolve_tender_sector

    user = MagicMock()
    user.industry = "Construction"
    data = {"sector": "Healthcare"}
    assert _resolve_tender_sector(user, data) == "Healthcare"
    assert data["sector"] == "Healthcare"


def test_checkout_gate_accepts_industry_key_and_guest_checkout():
    """`industry` is accepted as an alias; a guest (no user row) still resolves."""
    from app.api.stripe_checkout import _resolve_tender_sector

    assert _resolve_tender_sector(None, {"industry": "IT"}) == "IT"
