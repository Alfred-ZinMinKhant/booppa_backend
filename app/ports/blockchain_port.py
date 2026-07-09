from abc import ABC, abstractmethod
from typing import Any

class BlockchainPort(ABC):
    @abstractmethod
    def anchor_evidence(self, evidence_data: dict[str, Any], vendor_id: str | None = None) -> str:
        pass
