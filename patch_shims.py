import os

storage_shim = """\"\"\"Backward compatibility shim. Use app.core.providers.get_storage() directly.\"\"\"
from app.ports.storage_port import StoragePort

class S3Service:
    def __new__(cls) -> StoragePort:
        from app.core.providers import get_storage
        return get_storage()
"""
with open("app/services/storage.py", "w") as f:
    f.write(storage_shim)

email_shim = """\"\"\"Backward compatibility shim. Use app.core.providers.get_email() directly.\"\"\"
from app.ports.email_port import EmailPort

class EmailService:
    def __new__(cls) -> EmailPort:
        from app.core.providers import get_email
        return get_email()
"""
with open("app/services/email_service.py", "w") as f:
    f.write(email_shim)

blockchain_shim = """\"\"\"Backward compatibility shim. Use app.core.providers.get_blockchain() directly.\"\"\"
from app.ports.blockchain_port import BlockchainPort

class BlockchainService:
    def __new__(cls) -> BlockchainPort:
        from app.core.providers import get_blockchain
        return get_blockchain()
"""
with open("app/services/blockchain.py", "w") as f:
    f.write(blockchain_shim)
