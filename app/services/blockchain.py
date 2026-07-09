"""Backward compatibility shim. Use app.core.providers.get_blockchain() directly."""
from app.ports.blockchain_port import BlockchainPort
from app.adapters.polygon_blockchain import _raw_txn  # re-export for tests

class BlockchainService:
    def __new__(cls) -> BlockchainPort:
        from app.core.providers import get_blockchain
        return get_blockchain()
