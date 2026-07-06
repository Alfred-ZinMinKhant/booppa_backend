"""Transaction-value guards shared across evidence/proof renderers.

Dependency-free on purpose: PDF generators import this without pulling in web3.

Motivation (GTM readiness finding, Issue C): a customer-facing document must
never present a non-on-chain value — a simulated/QA session id such as
``admin-sim-…``, a ``PENDING`` sentinel, ``None``, or a raw report/order id — as
a blockchain transaction, and must not attach anchoring / proof / legal-standing
language to anything that is not a real, confirmed on-chain tx. `is_real_onchain_tx`
is the single gate every "anchored" / "Transaction:" render should pass through.
"""
from typing import Any

# A Polygon tx hash is 0x followed by 64 hex chars (32 bytes) → 66 chars total.
_TX_HEX_LEN = 66


def is_real_onchain_tx(tx: Any) -> bool:
    """True only for a well-formed on-chain transaction hash.

    Rejects None, ``PENDING``, ``admin-sim-…`` session ids, order references,
    and any value that isn't a 0x-prefixed 32-byte hex string. Note this is a
    *shape* check — it does not (and cannot, offline) prove the tx is mined; it
    exists to stop non-tx sentinels from ever being rendered as proof.
    """
    if not isinstance(tx, str):
        return False
    tx = tx.strip()
    if len(tx) != _TX_HEX_LEN:
        return False
    if not tx.lower().startswith("0x"):
        return False
    try:
        int(tx[2:], 16)
    except ValueError:
        return False
    return True
