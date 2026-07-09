"""Backward compatibility shim.

EmailService is a concrete subclass of ResendEmailAdapter so that:
  - Production code using EmailService() gets a fully functional email sender
  - Tests can monkeypatch EmailService._send_resend / _send_ses / send_html_email
  - Tests can import _filter_attachments and _MAX_ATTACHMENT_BYTES from this module
  - Tests can patch `es.httpx` and `es.settings` (re-exported module refs)

Use app.core.providers.get_email() for proper dependency injection.
"""
import httpx  # noqa: F401 — re-exported so tests can patch es.httpx.AsyncClient
from app.core.config import settings  # noqa: F401 — re-exported so tests can patch es.settings
from app.adapters.resend_email import (  # noqa: F401 — re-exported for test imports
    ResendEmailAdapter,
    _filter_attachments,
    _MAX_ATTACHMENT_BYTES,
)


class EmailService(ResendEmailAdapter):
    """Thin subclass of ResendEmailAdapter for backward-compat and test patching."""
    pass
