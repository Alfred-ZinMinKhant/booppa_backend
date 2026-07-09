"""Webhook idempotency: ProcessedWebhookEvent dedupe."""
from unittest.mock import AsyncMock

from tests.fixtures.stripe_events import wrap_event


def test_duplicate_event_id_skipped(
    client, post_webhook, stripe_session_factory, mocker
):
    """Same event_id delivered twice → second response says already_processed
    and the fulfillment handler is invoked only once."""
    fake_activate = AsyncMock(return_value=None)
    mocker.patch("app.api.stripe_webhook._activate_subscription", fake_activate)

    session = stripe_session_factory("vendor_active_monthly")
    event = wrap_event(session, event_id="evt_test_dedupe_42")

    r1 = post_webhook(event)
    r2 = post_webhook(event)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("status") == "already_processed"
    fake_activate.assert_awaited_once()
