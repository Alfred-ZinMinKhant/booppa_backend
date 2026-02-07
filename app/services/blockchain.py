from typing import Any, Dict, Optional

from web3 import Web3
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class BlockchainService:
    """Polygon blockchain service for evidence anchoring"""

    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(settings.POLYGON_RPC_URL))
        self.contract_address = settings.ANCHOR_CONTRACT_ADDRESS

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
        return Web3.to_bytes(hexstr="0x" + hex_value)

    def _get_private_key(self) -> str:
        key = settings.BLOCKCHAIN_PRIVATE_KEY
        if not key:
            raise RuntimeError("BLOCKCHAIN_PRIVATE_KEY is not configured")
        return key

    async def anchor_evidence(self, evidence_hash: str, metadata: str = "") -> str:
        """Anchor evidence hash on Polygon blockchain"""
        try:
            file_hash = self._hash_to_bytes32(evidence_hash)
            private_key = self._get_private_key()
            account = self.w3.eth.account.from_key(private_key)

            nonce = self.w3.eth.get_transaction_count(account.address)
            txn = self.contract.functions.anchorHash(file_hash, metadata).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "gas": 250000,
                    "gasPrice": self.w3.eth.gas_price,
                }
            )
            signed = self.w3.eth.account.sign_transaction(txn, private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            tx_hex = tx_hash.hex()

            logger.info("Evidence anchored on blockchain: %s", tx_hex)
            return tx_hex

        except Exception as e:
            logger.error("Blockchain anchoring failed: %s", e)
            raise

    def get_anchor_status(self, evidence_hash: str, tx_hash: Optional[str] = None) -> Dict[str, Any]:
        """Verify if evidence is anchored on blockchain and optionally confirm tx."""
        anchored = False
        anchored_at = None
        tx_confirmed = None
        try:
            file_hash = self._hash_to_bytes32(evidence_hash)
            anchored, anchored_at = self.contract.functions.isAnchored(file_hash).call()
        except Exception as e:
            logger.error("Blockchain anchor status failed: %s", e)

        if tx_hash:
            try:
                receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                tx_confirmed = bool(receipt and receipt.status == 1)
            except Exception as e:
                logger.warning("Transaction receipt lookup failed: %s", e)

        return {
            "anchored": bool(anchored),
            "anchored_at": anchored_at,
            "tx_confirmed": tx_confirmed,
        }
