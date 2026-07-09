with open("app/adapters/s3_storage.py", "r") as f:
    content = f.read()
content = content.replace("class S3Service:", "from app.ports.storage_port import StoragePort\n\nclass S3StorageAdapter(StoragePort):")
with open("app/adapters/s3_storage.py", "w") as f:
    f.write(content)

with open("app/adapters/resend_email.py", "r") as f:
    content = f.read()
content = content.replace("class EmailService:", "from app.ports.email_port import EmailPort\n\nclass ResendEmailAdapter(EmailPort):")
with open("app/adapters/resend_email.py", "w") as f:
    f.write(content)

with open("app/adapters/polygon_blockchain.py", "r") as f:
    content = f.read()
content = content.replace("class BlockchainService:", "from app.ports.blockchain_port import BlockchainPort\n\nclass PolygonBlockchainAdapter(BlockchainPort):")
with open("app/adapters/polygon_blockchain.py", "w") as f:
    f.write(content)
