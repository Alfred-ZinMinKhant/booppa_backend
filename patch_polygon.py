import re

with open('app/adapters/polygon_blockchain.py', 'r') as f:
    content = f.read()

# 1. async def get_anchor_status
content = content.replace(
    'def get_anchor_status(self, evidence_hash: str, tx_hash: Optional[str] = None) -> Dict[str, Any]:',
    'async def get_anchor_status(self, evidence_hash: str, tx_hash: Optional[str] = None) -> Dict[str, Any]:'
)

# 2. Await get_anchor_status calls
content = content.replace(
    'status = self.get_anchor_status(evidence_hash)',
    'status = await self.get_anchor_status(evidence_hash)'
)
content = content.replace(
    'status = self.get_anchor_status(h)',
    'status = await self.get_anchor_status(h)'
)

# 3. to_thread for contract call
content = content.replace(
    'anchored, raw_ts = self.contract.functions.isAnchored(file_hash).call()',
    'import asyncio\n            anchored, raw_ts = await asyncio.to_thread(self.contract.functions.isAnchored(file_hash).call)'
)

# 4. to_thread for get_transaction_receipt
content = content.replace(
    'receipt = self.w3.eth.get_transaction_receipt(tx_hash)',
    'import asyncio\n                receipt = await asyncio.to_thread(self.w3.eth.get_transaction_receipt, tx_hash)'
)

# 5. to_thread for get_transaction_count (both variants)
content = content.replace(
    "nonce = self.w3.eth.get_transaction_count(account.address, 'pending')",
    "import asyncio\n            nonce = await asyncio.to_thread(self.w3.eth.get_transaction_count, account.address, 'pending')"
)
content = content.replace(
    "nonce = self.w3.eth.get_transaction_count(account.address)",
    "import asyncio\n            nonce = await asyncio.to_thread(self.w3.eth.get_transaction_count, account.address)"
)

# 6. to_thread for send_raw_transaction
content = content.replace(
    "tx_hash = self.w3.eth.send_raw_transaction(_raw_txn(signed))",
    "import asyncio\n            tx_hash = await asyncio.to_thread(self.w3.eth.send_raw_transaction, _raw_txn(signed))"
)

with open('app/adapters/polygon_blockchain.py', 'w') as f:
    f.write(content)
