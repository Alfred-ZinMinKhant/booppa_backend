"""Admin test-checkout reports must anchor with a MOCK tx hash (no gas).

Real customers and QA test checkouts share one Polygon Amoy gas wallet. Test
runs used to anchor for real and drained it, which is what starved real buyers'
reports during the 2026-07-24 out-of-gas outage. Reports tagged
``assessment_data["test_simulation"]`` now pass ``demo=True`` into
``anchor_evidence``, which returns ``demo_tx_hash(...)`` and never builds or
sends a transaction.

These pin the contract at the adapter level. The instance is built without
__init__ (the demo branch runs before any w3/contract access, and __init__
needs live chain config we don't want in a unit test).
"""
import asyncio

from app.adapters.polygon_blockchain import PolygonBlockchainAdapter
from app.services.blockchain import BlockchainService
from app.services.supplier_due_diligence_generator import demo_tx_hash
from app.core.demo_flags import is_demo_anchor, is_demo_session, is_demo_data

_HASH = "a" * 64  # valid 64-char SHA-256 hex


def _bare_service():
    # Skip __init__ (needs contract address / RPC config); demo path never
    # touches self.w3 or self.contract.
    return object.__new__(BlockchainService)


def test_demo_returns_mock_hash_without_touching_chain():
    svc = _bare_service()

    # Trip-wire: if the demo branch fell through to the real path it would call
    # these on a bare instance (no self.w3) and raise AttributeError, which is a
    # louder failure than a wrong return — but assert the value too.
    tx = asyncio.run(svc.anchor_evidence(_HASH, metadata="notarization:test", demo=True))

    assert tx == demo_tx_hash(_HASH)
    assert tx.startswith("0x") and len(tx) == 66


def test_demo_is_deterministic_per_hash():
    svc = _bare_service()
    a = asyncio.run(svc.anchor_evidence(_HASH, demo=True))
    b = asyncio.run(svc.anchor_evidence(_HASH, demo=True))
    assert a == b


def test_real_path_is_taken_when_demo_false(monkeypatch):
    """demo=False must NOT short-circuit — it enters the real anchoring code
    (which here fails fast on the bare instance's missing get_anchor_status,
    proving the mock branch was not taken)."""
    svc = _bare_service()

    sentinel = {"called": False}

    async def _fake_status(self, h):
        sentinel["called"] = True
        return {"anchored": False}

    # Patch the first thing the real path awaits so we don't need a chain, and
    # can prove the real branch (not the demo branch) executed.
    monkeypatch.setattr(PolygonBlockchainAdapter, "get_anchor_status", _fake_status)

    # It will still raise later (no self.w3), but only AFTER entering the real
    # path — which is all we're asserting.
    try:
        asyncio.run(svc.anchor_evidence(_HASH, demo=False))
    except Exception:
        pass

    assert sentinel["called"], "demo=False should take the real anchoring path"


# ── is_demo_anchor helper: the signal the call sites depend on ────────────────

def test_demo_flag_detects_admin_sim_session():
    assert is_demo_session("admin-sim-abc123")
    assert is_demo_anchor(session_id="admin-sim-xyz")
    assert not is_demo_session("cs_test_live_realstripe")
    assert not is_demo_anchor(session_id="cs_live_realstripe")
    assert not is_demo_session(None)


def test_demo_flag_detects_test_simulation_dict():
    assert is_demo_data({"test_simulation": True})
    assert is_demo_anchor(assessment={"test_simulation": True})
    assert is_demo_anchor(metadata={"test_simulation": "1"})
    assert not is_demo_data({"test_simulation": False})
    assert not is_demo_data({})
    assert not is_demo_data(None)


def test_demo_flag_all_signals_default_false():
    # A real purchase supplies none of the signals.
    assert not is_demo_anchor()
    assert not is_demo_anchor(assessment={"payment_confirmed": True}, session_id="cs_live_x")
    # Explicit is_test wins when a caller already resolved it.
    assert is_demo_anchor(is_test=True)
