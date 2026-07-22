"""Shared canonical legal-name resolver (Phase 5 of the TRM MAS-defensibility work).

Kills the recurring "Assessed Entity: thunes.com" bug class by giving every
generator one field (`User.legal_name`) to read instead of independently
guessing a display name from a raw `company` value.
"""
import pytest

from tests._test_helpers import make_user


@pytest.mark.asyncio
async def test_resolve_legal_name_uen_hit(test_db, mocker):
    from app.core.models import DiscoveredVendor
    from app.services.evidence_enricher import resolve_legal_name

    user = make_user(test_db, company="acme.com")
    user.uen = "201912345A"
    test_db.commit()

    dv = DiscoveredVendor(uen="201912345A", company_name="ACME PTE. LTD.", source="test")
    test_db.add(dv)
    test_db.commit()

    name = await resolve_legal_name(user, test_db)
    assert name == "ACME PTE. LTD."
    test_db.refresh(user)
    assert user.legal_name == "ACME PTE. LTD."


@pytest.mark.asyncio
async def test_resolve_legal_name_fuzzy_live_hit(test_db, mocker):
    from app.services.evidence_enricher import resolve_legal_name

    user = make_user(test_db, company="Nova Pay")

    mocker.patch(
        "app.services.evidence_enricher.fetch_acra_status",
        return_value={"found": True, "uen": "202099999Z", "registered_name": "NOVAPAY PTE. LTD."},
    )

    name = await resolve_legal_name(user, test_db, company_hint="Nova Pay")
    assert name == "NOVAPAY PTE. LTD."
    test_db.refresh(user)
    assert user.legal_name == "NOVAPAY PTE. LTD."
    assert user.uen == "202099999Z"


@pytest.mark.asyncio
async def test_resolve_legal_name_no_match_falls_back(test_db, mocker):
    from app.services.evidence_enricher import resolve_legal_name

    user = make_user(test_db, company="thunes.com")
    mocker.patch(
        "app.services.evidence_enricher.fetch_acra_status",
        return_value={"found": False},
    )

    name = await resolve_legal_name(user, test_db, company_hint="thunes.com")
    assert name == "thunes.com"
    test_db.refresh(user)
    assert user.legal_name is None


def test_display_legal_name_prefers_resolved_over_raw():
    from app.services.evidence_enricher import display_legal_name

    class _U:
        legal_name = "NOVAPAY PTE. LTD."
        company = "novapay.io"

    assert display_legal_name(_U()) == "NOVAPAY PTE. LTD."

    class _V:
        legal_name = None
        company = "novapay.io"

    assert display_legal_name(_V()) == "novapay.io"

    class _W:
        legal_name = None
        company = None

    assert display_legal_name(_W()) == "Your Organisation"
