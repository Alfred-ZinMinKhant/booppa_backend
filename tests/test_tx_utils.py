"""`is_real_onchain_tx` is the single gate that keeps non-transaction values
(session ids like ``admin-sim-…``, ``PENDING`` sentinels, order refs) from ever
being rendered as blockchain proof on customer-facing documents."""
from app.services.tx_utils import is_real_onchain_tx


def test_accepts_wellformed_tx():
    assert is_real_onchain_tx("0x" + "a" * 64)
    assert is_real_onchain_tx("0x" + "0" * 64)  # demo/deterministic still 0x-shaped


def test_rejects_simulated_session_id():
    assert not is_real_onchain_tx("admin-sim-beb1702e-1a2b-3c4d")


def test_rejects_sentinels_and_none():
    for bad in (None, "", "PENDING", "Pending", "pending"):
        assert not is_real_onchain_tx(bad)


def test_rejects_malformed_hex_and_wrong_length():
    assert not is_real_onchain_tx("0x" + "g" * 64)   # non-hex
    assert not is_real_onchain_tx("0xabc")            # too short
    assert not is_real_onchain_tx("a" * 66)           # no 0x prefix
    assert not is_real_onchain_tx(12345)              # not a string
