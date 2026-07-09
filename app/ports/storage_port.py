from abc import ABC, abstractmethod

class StoragePort(ABC):
    @abstractmethod
    async def upload_pdf(self, pdf_bytes: bytes, report_id: str) -> str:
        pass
