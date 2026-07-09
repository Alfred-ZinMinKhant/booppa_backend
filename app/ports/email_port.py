from abc import ABC, abstractmethod
from typing import Optional

Attachment = tuple[str, bytes]

class EmailPort(ABC):
    @abstractmethod
    async def send_html_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: Optional[list[Attachment]] = None,
    ) -> bool:
        pass
