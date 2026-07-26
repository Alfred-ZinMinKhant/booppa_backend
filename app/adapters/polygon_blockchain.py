from typing import Any, Dict, Optional

from web3 import Web3
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

# How long to wait for an anchor tx receipt before giving up. A timeout is
# treated as a failure (unknown != confirmed) so the caller's retry/sweep path
# runs; the tx hex is logged so an eventually-mined tx stays traceable.
ANCHOR_RECEIPT_TIMEOUT_SECONDS = 180


def _raw_txn(signed) -> bytes:
    """Return the raw signed-transaction bytes across web3.py versions.

    web3.py >= 6 renamed SignedTransaction.rawTransaction -> raw_transaction
    (snake_case). Installed here is 7.x, where only raw_transaction exists;
    the old camelCase attribute raised
    "'SignedTransaction' object has no attribute 'rawTransaction'" and broke
    every real (demo=False) anchor. Prefer the new name, fall back to the old.
    """
    return getattr(signed, "raw_transaction", None) or signed.rawTransaction


from app.ports.blockchain_port import BlockchainPort

class PolygonBlockchainAdapter(BlockchainPort):
    """Polygon blockchain service for evidence anchoring"""

    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(settings.active_polygon_rpc_url))
        self.contract_address = settings.active_anchor_contract_address

        # ABI for EvidenceAnchorV3 contract
        self.contract_abi = [
            {
                "inputs": [
                    {"internalType": "bytes32", "name": "fileHash", "type": "bytes32"},
                    {"internalType": "string", "name": "metadata", "type": "string"},
                ],
                "name": "anchorHash",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
            {
                "inputs": [{"internalType": "bytes32", "name": "fileHash", "type": "bytes32"}],
                "name": "isAnchored",
                "outputs": [
                    {"internalType": "bool", "name": "", "type": "bool"},
                    {"internalType": "uint256", "name": "", "type": "uint256"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "internalType": "bytes32", "name": "fileHash", "type": "bytes32"},
                    {"indexed": True, "internalType": "address", "name": "anchoredBy", "type": "address"},
                    {"indexed": False, "internalType": "uint256", "name": "timestamp", "type": "uint256"},
                    {"indexed": False, "internalType": "string", "name": "metadata", "type": "string"},
                ],
                "name": "HashAnchored",
                "type": "event",
            }
        ]
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.contract_address),
            abi=self.contract_abi,
        )

    def _hash_to_bytes32(self, evidence_hash: str) -> bytes:
        hex_value = evidence_hash.strip().lower()
        if hex_value.startswith("0x"):
            hex_value = hex_value[2:]
        if not hex_value or len(hex_value) != 64 or not all(c in "0123456789abcdef" for c in hex_value):
            raise ValueError(
                f"Invalid evidence_hash: expected a 64-char SHA-256 hex string, "
                f"got {len(hex_value)!r}-char value {hex_value[:16]!r}{'...' if len(hex_value) > 16 else ''}"
            )
        return Web3.to_bytes(hexstr="0x" + hex_value)

    def _get_private_key(self) -> str:
        key = settings.BLOCKCHAIN_PRIVATE_KEY
        if not key:
            raise RuntimeError("BLOCKCHAIN_PRIVATE_KEY is not configured")
        return key

    async def _confirm_receipt(self, tx_hash, tx_hex: str) -> None:
        """Block until the tx is mined and raise unless it succeeded.

        ``send_raw_transaction`` only proves the tx was broadcast — a tx that
        reverts on-chain (e.g. duplicate hash, onlyOwner, out of gas) still
        returns a normal-looking hash. Mirrors the ``receipt.status == 1``
        check in ``get_anchor_status``, run inline instead of on demand.
        """
        import asyncio
        try:
            receipt = await asyncio.to_thread(
                self.w3.eth.wait_for_transaction_receipt, tx_hash, ANCHOR_RECEIPT_TIMEOUT_SECONDS
            )
        except Exception as e:
            # Timed out / RPC error: unknown is not confirmed. Log the hex so an
            # eventually-mined tx can still be reconciled by hand.
            logger.error("Anchor tx %s not confirmed within timeout: %s", tx_hex, e)
            raise
        if receipt.status != 1:
            raise RuntimeError(f"Anchor tx {tx_hex} reverted on-chain (status={receipt.status})")

    async def anchor_evidence(self, evidence_hash: str, metadata: str = "", force: bool = False, demo: bool = False) -> str:
        """Anchor evidence hash on Polygon blockchain.

        Pass ``force=True`` when the caller needs a fresh tx_hash even if the
        hash is already on-chain (e.g. signed-Cover-Sheet anchoring, which has
        to surface a tx for the buyer to see). This skips the local idempotency
        check, which exists to save gas.

        Note the contract is *not* additive: ``anchorHash()`` has
        ``require(anchoredTimestamps[fileHash] == 0)``, so re-anchoring a hash
        that is already on-chain reverts when mined. ``send_raw_transaction``
        only confirms the tx was broadcast, so this method waits for the
        receipt and raises when ``status != 1`` rather than returning a
        tx_hash that resolves to a failed transaction on Polygonscan. Callers'
        existing retry / ``anchor_failed`` handling then applies.

        Pass ``demo=True`` for admin test-checkout / preview reports: return a
        deterministic mock tx hash (``demo_tx_hash``) without building or
        sending a transaction, so QA runs never spend real gas from the shared
        wallet. This mirrors the subscription buyer-preview path.
        """
        if demo:
            from app.services.supplier_due_diligence_generator import demo_tx_hash
            tx_hex = demo_tx_hash(evidence_hash)
            logger.info("Evidence anchored in DEMO mode (no gas): %s", tx_hex)
            return tx_hex
        try:
            # Idempotency check: Don't spend gas if already anchored, unless
            # the caller explicitly wants a fresh tx (force=True).
            if not force:
                status = await self.get_anchor_status(evidence_hash)
                if status.get("anchored"):
                    logger.info(f"Evidence {evidence_hash} already anchored. Skipping transaction.")
                    return None  # already on-chain; no new tx_hash available

            file_hash = self._hash_to_bytes32(evidence_hash)
            private_key = self._get_private_key()
            account = self.w3.eth.account.from_key(private_key)

            import asyncio
            nonce = await asyncio.to_thread(self.w3.eth.get_transaction_count, account.address, 'pending')
            txn = self.contract.functions.anchorHash(file_hash, metadata).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "gas": 250000,
                    "gasPrice": self.w3.eth.gas_price,
                }
            )
            signed = self.w3.eth.account.sign_transaction(txn, private_key)
            import asyncio
            tx_hash = await asyncio.to_thread(self.w3.eth.send_raw_transaction, _raw_txn(signed))
            tx_hex = tx_hash.hex()

            await self._confirm_receipt(tx_hash, tx_hex)

            logger.info("Evidence anchored and confirmed on blockchain: %s", tx_hex)
            return tx_hex

        except Exception as e:
            logger.error("Blockchain anchoring failed: %s", e)
            raise

    # NOTE: batch_anchor_hashes was removed (2026-07-26). It had zero callers
    # and could never have worked: its ABI entry declared
    # batchAnchor(bytes32[], string) while EvidenceAnchorV3 actually exposes
    # anchorBatch(bytes32[] fileHashes, string[] metadata) — wrong name and
    # wrong signature. Re-add it against the real contract signature if batching
    # is ever needed, and give it the same _confirm_receipt gate as anchor_evidence.

    async def get_anchor_status(self, evidence_hash: str, tx_hash: Optional[str] = None) -> Dict[str, Any]:
        """Verify if evidence is anchored on blockchain and optionally confirm tx."""
        anchored = False
        anchored_at = None
        tx_confirmed = None
        try:
            file_hash = self._hash_to_bytes32(evidence_hash)
            import asyncio
            anchored, raw_ts = await asyncio.to_thread(self.contract.functions.isAnchored(file_hash).call)
            if anchored and raw_ts:
                from datetime import datetime, timezone
                anchored_at = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).isoformat()
        except Exception as e:
            logger.error("Blockchain anchor status failed: %s", e)

        if tx_hash:
            try:
                import asyncio
                receipt = await asyncio.to_thread(self.w3.eth.get_transaction_receipt, tx_hash)
                tx_confirmed = bool(receipt and receipt.status == 1)
            except Exception as e:
                logger.warning("Transaction receipt lookup failed: %s", e)

        return {
            "anchored": bool(anchored),
            "anchored_at": anchored_at,
            "tx_confirmed": tx_confirmed,
        }
