from abc import ABC, abstractmethod
from typing import Any

class BlockchainPort(ABC):
    @abstractmethod
    async def anchor_evidence(self, evidence_hash: str, metadata: str = "", force: bool = False) -> str:
        pass
