"""Backward compatibility shim. Use app.core.providers.get_blockchain() directly."""
from app.adapters.polygon_blockchain import PolygonBlockchainAdapter, _raw_txn  # re-export for tests

class BlockchainService(PolygonBlockchainAdapter):
    pass
