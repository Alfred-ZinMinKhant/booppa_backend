"""
Webhook Service — V12
Delivers HMAC-SHA256 signed outbound webhooks to organisation endpoints.

Single, synchronous delivery path shared by:
  * the manual `test.ping` endpoint (`app/api/vendor_features.py`), and
  * real event emission from Celery (`emit_webhook_task` in `app/workers/tasks.py`).

Callers are all synchronous (Celery tasks, the Stripe webhook handler, a FastAPI
route), so this module stays sync — no event loop juggling.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.models import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

TIMEOUT = 10.0


def _hmac_sign(secret: str, body: bytes) -> str:
    return hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()


def deliver(
    db: Session,
    endpoint: WebhookEndpoint,
    event_type: str,
    body: Dict[str, Any],
) -> WebhookDelivery:
    """POST a single already-final body dict to one endpoint, signed + logged."""
    body_bytes = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    signature = _hmac_sign(endpoint.secret, body_bytes)
    status_code: Optional[int] = None
    resp_body: Optional[str] = None
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                endpoint.url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Booppa-Event": event_type,
                    "X-Booppa-Signature": f"sha256={signature}",
                    "User-Agent": "Booppa-Webhooks/1.0",
                },
            )
        status_code = resp.status_code
        resp_body = (resp.text or "")[:2048]
        logger.info("Webhook %s → %s: %d", event_type, endpoint.url, status_code)
    except Exception as e:  # network failure — logged, never raised
        resp_body = f"network_error: {e}"
        logger.warning("Webhook delivery failed for %s: %s", endpoint.url, e)

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type=event_type,
        payload=body,
        status_code=status_code,
        response_body=resp_body,
        success=(status_code is not None and 200 <= status_code < 300),
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def dispatch_event(
    db: Session,
    organisation_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> int:
    """Fire `event_type` to every active endpoint of an org subscribed to it.

    Returns the number of endpoints delivered to. Never raises on delivery
    failure — each attempt is logged as a WebhookDelivery row.
    """
    endpoints = (
        db.query(WebhookEndpoint)
        .filter(
            WebhookEndpoint.organisation_id == organisation_id,
            WebhookEndpoint.is_active == True,  # noqa: E712
        )
        .all()
    )
    envelope = {
        "event": event_type,
        "data": payload,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    delivered = 0
    for ep in endpoints:
        subscribed = ep.events or []
        if subscribed and event_type not in subscribed:
            continue
        deliver(db, ep, event_type, envelope)
        delivered += 1
    return delivered
