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


# ── Async-context regression ─────────────────────────────────────────────────
# Every Celery fulfillment workflow runs under `asyncio.run(...)`, so anything
# it calls sees a live event loop. `display_legal_name` used to build its
# coroutines first and let `asyncio.run()` reject them, which (a) leaked two
# "coroutine ... was never awaited" RuntimeWarnings into the worker logs and
# (b) silently skipped the ACRA backfill on every PDPA / Vendor Proof /
# notarization document. These pin both halves of that fix.


@pytest.mark.asyncio
async def test_display_legal_name_in_async_context_emits_no_runtime_warning(
    test_db, mocker, caplog
):
    """Sync display_legal_name must not construct un-awaited coroutines when a
    loop is already running. Fails on the pre-fix implementation."""
    import warnings

    from app.services.evidence_enricher import display_legal_name

    user = make_user(test_db, company="novapay.io")
    resolver = mocker.patch("app.services.evidence_enricher.resolve_legal_name")

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level("WARNING", logger="app.services.evidence_enricher"):
            name = display_legal_name(user, test_db)

    # Falls back rather than raising, and says so loudly enough to find in logs.
    assert name == "novapay.io"
    assert "async context" in caplog.text
    # The coroutine must never even be created on this path.
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_display_legal_name_resolves_inside_async_context(test_db, mocker):
    """The async variant is what actually performs the backfill that the sync
    entry point could never reach from a Celery workflow."""
    from app.services.evidence_enricher import resolve_display_legal_name

    user = make_user(test_db, company="Nova Pay")
    mocker.patch(
        "app.services.evidence_enricher.fetch_acra_status",
        return_value={
            "found": True,
            "uen": "202099999Z",
            "registered_name": "NOVAPAY PTE. LTD.",
        },
    )

    name = await resolve_display_legal_name(user, test_db)
    assert name == "NOVAPAY PTE. LTD."
    test_db.refresh(user)
    assert user.legal_name == "NOVAPAY PTE. LTD."


@pytest.mark.asyncio
async def test_resolve_display_legal_name_falls_back_on_timeout(test_db, mocker, caplog):
    """A stalled ACRA lookup must not hold a paid PDF open past the deadline."""
    import asyncio

    from app.services.evidence_enricher import resolve_display_legal_name

    user = make_user(test_db, company="novapay.io")

    async def _hang(*args, **kwargs):
        await asyncio.sleep(30)

    mocker.patch("app.services.evidence_enricher.fetch_acra_status", side_effect=_hang)

    with caplog.at_level("WARNING", logger="app.services.evidence_enricher"):
        name = await resolve_display_legal_name(user, test_db, timeout=1)

    assert name == "novapay.io"
    assert "timed out" in caplog.text


@pytest.mark.asyncio
async def test_resolve_display_legal_name_skips_lookup_when_already_resolved(
    test_db, mocker
):
    """Already-resolved users must not trigger a redundant ACRA call."""
    from app.services.evidence_enricher import resolve_display_legal_name

    user = make_user(test_db, company="novapay.io")
    user.legal_name = "NOVAPAY PTE. LTD."
    test_db.commit()

    resolver = mocker.patch("app.services.evidence_enricher.resolve_legal_name")

    assert await resolve_display_legal_name(user, test_db) == "NOVAPAY PTE. LTD."
    resolver.assert_not_called()
