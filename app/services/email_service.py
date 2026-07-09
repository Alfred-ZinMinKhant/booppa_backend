"""Backward compatibility shim. Use app.core.providers.get_email() directly."""
from app.ports.email_port import EmailPort

class EmailService:
    def __new__(cls) -> EmailPort:
        from app.core.providers import get_email
        return get_email()
