"""Pre-payment ACRA gate for Vendor Proof checkout (block only when UEN given)."""
import asyncio

import pytest
from fastapi import HTTPException

from app.api.stripe_checkout import _gate_acra_live


def _patch_acra(monkeypatch, result):
    async def _fake(uen):
        return result
    # _gate_acra_live imports fetch_acra_status from evidence_enricher at call time.
    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _fake)


def test_struck_off_uen_blocks_with_409(monkeypatch):
    _patch_acra(monkeypatch, {"found": True, "live": False, "entity_status": "Struck Off"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_gate_acra_live("201912345A"))
    assert exc.value.status_code == 409
    assert "Struck Off" in exc.value.detail


def test_unknown_uen_blocks_with_422(monkeypatch):
    _patch_acra(monkeypatch, {"found": False})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_gate_acra_live("999999999Z"))
    assert exc.value.status_code == 422
    assert "ACRA" in exc.value.detail


def test_live_uen_passes(monkeypatch):
    _patch_acra(monkeypatch, {"found": True, "live": True, "entity_status": "Live"})
    # Should not raise.
    asyncio.run(_gate_acra_live("201912345A"))


def test_lookup_error_is_non_fatal(monkeypatch):
    async def _boom(uen):
        raise RuntimeError("data.gov.sg down")
    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _boom)
    # A lookup outage must not block a paying customer — falls back to warn path.
    asyncio.run(_gate_acra_live("201912345A"))
