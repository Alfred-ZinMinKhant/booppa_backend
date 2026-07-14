"""Email suppression list + unsubscribe tokens.

Single source of truth for "may we email this address?". Two suppression
scopes:

- ``all``       — hard bounce or spam complaint (from SES via SNS). Never email
                  this address again, transactional or not.
- ``marketing`` — the recipient used one-click List-Unsubscribe. Only recurring
                  / marketing sends stop; transactional receipts still flow.

``send_html_email`` calls :func:`is_suppressed` before every dispatch. The
SNS webhook (``app/api/email_sns.py``) and the unsubscribe endpoint
(``app/api/email_unsubscribe.py``) call :func:`add_suppression`.

Unsubscribe links are stateless one-click tokens: an HMAC of the lowercased
email under ``SECRET_KEY``. No DB lookup needed to validate the click, and the
token is unforgeable without the secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# Categories a caller can tag a send with. Recurring digests / marketing pass
# "marketing"; everything else defaults to "transactional".
MARKETING = "marketing"
TRANSACTIONAL = "transactional"


def normalize(email: str) -> str:
    return (email or "").strip().lower()


# ── Suppression checks / writes ────────────────────────────────────────────

def is_suppressed(email: str, category: str = TRANSACTIONAL) -> bool:
    """Return True if we must not send to ``email`` for this ``category``.

    A ``scope="all"`` row blocks every category. A ``scope="marketing"`` row
    blocks only ``category="marketing"``. Fails open (returns False) if the
    lookup errors — a suppression-table outage must never silently drop
    transactional receipts.
    """
    addr = normalize(email)
    if not addr:
        return False
    try:
        from app.core.db import SessionLocal
        from app.core.models import EmailSuppression

        db = SessionLocal()
        try:
            rows = (
                db.query(EmailSuppression.scope)
                .filter(EmailSuppression.email == addr)
                .all()
            )
        finally:
            db.close()
        scopes = {r[0] for r in rows}
        if "all" in scopes:
            return True
        if category == MARKETING and "marketing" in scopes:
            return True
        return False
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[Suppression] lookup failed for %s: %s", addr, e)
        return False


def add_suppression(email: str, scope: str, source: str, reason: str | None = None) -> bool:
    """Idempotently record a suppression. Returns True on success."""
    addr = normalize(email)
    if not addr:
        return False
    try:
        from app.core.db import SessionLocal
        from app.core.models import EmailSuppression

        db = SessionLocal()
        try:
            exists = (
                db.query(EmailSuppression.id)
                .filter(
                    EmailSuppression.email == addr,
                    EmailSuppression.scope == scope,
                )
                .first()
            )
            if exists:
                return True
            db.add(
                EmailSuppression(
                    email=addr,
                    scope=scope,
                    source=source,
                    reason=(reason or "")[:500] or None,
                )
            )
            db.commit()
            logger.info("[Suppression] added %s scope=%s source=%s", addr, scope, source)
            return True
        finally:
            db.close()
    except Exception as e:
        logger.error("[Suppression] failed to add %s: %s", addr, e)
        return False


# ── One-click unsubscribe tokens ───────────────────────────────────────────

def make_unsubscribe_token(email: str) -> str:
    addr = normalize(email)
    mac = hmac.new(
        settings.SECRET_KEY.encode("utf-8"), addr.encode("utf-8"), hashlib.sha256
    ).digest()[:12]
    sig = base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(addr.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


def verify_unsubscribe_token(token: str) -> str | None:
    """Return the email the token authorises, or None if invalid."""
    try:
        payload, sig = (token or "").split(".", 1)
        pad = "=" * (-len(payload) % 4)
        addr = base64.urlsafe_b64decode(payload + pad).decode("utf-8")
        expected = make_unsubscribe_token(addr).split(".", 1)[1]
        if hmac.compare_digest(sig, expected):
            return addr
        return None
    except Exception:
        return None


def _public_base() -> str:
    return (settings.API_PUBLIC_BASE_URL or settings.VERIFY_BASE_URL).rstrip("/")


def unsubscribe_url(email: str) -> str:
    return f"{_public_base()}/api/email/unsubscribe?token={make_unsubscribe_token(email)}"


def list_unsubscribe_headers(email: str) -> dict[str, str]:
    """RFC 2369 + RFC 8058 one-click unsubscribe headers for a recipient."""
    return {
        "List-Unsubscribe": f"<{unsubscribe_url(email)}>, <mailto:{settings.SUPPORT_EMAIL}?subject=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
