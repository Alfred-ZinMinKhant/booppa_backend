"""Backward compatibility shim. Use app.core.providers.get_blockchain() directly."""
from app.ports.blockchain_port import BlockchainPort

class BlockchainService:
    def __new__(cls) -> BlockchainPort:
        from app.core.providers import get_blockchain
        return get_blockchain()
