"""Helpers to build Stripe `checkout.session.completed` event envelopes.

The session-object construction lives in conftest.py's `stripe_session_factory`
fixture; this module wraps a session dict in a full event for delivery to the
webhook handler.
"""
import time
import uuid
from typing import Any


def wrap_event(
    session_obj: dict[str, Any],
    *,
    event_type: str = "checkout.session.completed",
    event_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a checkout.session dict into a Stripe-shaped event envelope."""
    return {
        "id": event_id or f"evt_test_{uuid.uuid4().hex[:24]}",
        "object": "event",
        "api_version": "2024-04-10",
        "created": int(time.time()),
        "type": event_type,
        "livemode": False,
        "pending_webhooks": 0,
        "request": {"id": None, "idempotency_key": None},
        "data": {"object": session_obj},
    }
