"""
Webhook Service — V12
Delivers HMAC-SHA256 signed outbound webhooks to organisation endpoints.
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict

import httpx
from sqlalchemy.orm import Session

from app.core.models_enterprise import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

TIMEOUT = 10.0
MAX_RETRIES = 3


def _sign(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def dispatch_event(
    organisation_id: str,
    event_type: str,
    payload: Dict[str, Any],
    db: Session,
) -> None:
    """Fire webhook for all active endpoints subscribed to event_type."""
    endpoints = (
        db.query(WebhookEndpoint)
        .filter(
            WebhookEndpoint.organisation_id == organisation_id,
            WebhookEndpoint.is_active == True,
        )
        .all()
    )

    body = json.dumps({"event": event_type, "data": payload, "sent_at": datetime.utcnow().isoformat()})
    body_bytes = body.encode()

    for ep in endpoints:
        subscribed = ep.events or []
        if subscribed and event_type not in subscribed:
            continue
        await _deliver(ep, event_type, body_bytes, payload, db)


async def _deliver(
    endpoint: WebhookEndpoint,
    event_type: str,
    body_bytes: bytes,
    payload: dict,
    db: Session,
    attempt: int = 1,
) -> None:
    signature = _sign(body_bytes, endpoint.secret)
    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        endpoint_id=endpoint.id,
        event_type=event_type,
        payload=payload,
        attempt=attempt,
    )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                endpoint.url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Booppa-Signature": signature,
                    "X-Booppa-Event": event_type,
                },
            )
        delivery.status_code = resp.status_code
        delivery.response_body = resp.text[:500]
        delivery.success = 200 <= resp.status_code < 300
        logger.info("Webhook %s → %s: %d", event_type, endpoint.url, resp.status_code)
    except Exception as e:
        delivery.success = False
        delivery.response_body = str(e)[:500]
        logger.warning("Webhook delivery failed for %s: %s", endpoint.url, e)
    finally:
        db.add(delivery)
        db.commit()
