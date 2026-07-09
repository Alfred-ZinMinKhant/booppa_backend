"""Backward compatibility shim. Use app.core.providers.get_storage() directly."""
from app.ports.storage_port import StoragePort

class S3Service:
    def __new__(cls) -> StoragePort:
        from app.core.providers import get_storage
        return get_storage()
