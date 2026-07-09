"""Backward compatibility shim. Use app.core.providers.get_storage() directly."""
from app.adapters.s3_storage import S3StorageAdapter

class S3Service(S3StorageAdapter):
    pass
