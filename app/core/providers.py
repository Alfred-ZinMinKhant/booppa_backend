from app.ports.storage_port import StoragePort
from app.ports.email_port import EmailPort
from app.ports.blockchain_port import BlockchainPort

from app.adapters.s3_storage import S3StorageAdapter
from app.adapters.resend_email import ResendEmailAdapter
from app.adapters.polygon_blockchain import PolygonBlockchainAdapter

def get_storage() -> StoragePort:
    return S3StorageAdapter()

def get_email() -> EmailPort:
    return ResendEmailAdapter()

def get_blockchain() -> BlockchainPort:
    return PolygonBlockchainAdapter()
