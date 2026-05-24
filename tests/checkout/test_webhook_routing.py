"""Webhook routing tests.

Asserts that a signed `checkout.session.completed` event for each SKU dispatches
to the expected fulfillment handler. The handler itself is patched so this test
stays focused on routing — full fulfillment behavior is exercised in
tests/fulfillment/.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fixtures.product_catalog import ALL_SKUS, ONE_TIME, BUNDLES, SUBSCRIPTIONS, sku_id
from tests.fixtures.stripe_events import wrap_event


@pytest.mark.parametrize(
    "case", SUBSCRIPTIONS, ids=[sku_id(c) for c in SUBSCRIPTIONS]
)
def test_subscription_webhook_calls_activate(
    case, client, post_webhook, stripe_session_factory, mocker, email_capture
):
    """Subscription SKUs hit _activate_subscription synchronously."""
    fake_activate = AsyncMock(return_value=None)
    mocker.patch("app.api.stripe_webhook._activate_subscription", fake_activate)

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_activate.assert_awaited_once()
    kwargs = fake_activate.await_args.kwargs
    assert kwargs["product_type"] == case.product_type
    assert kwargs["customer_email"] == session["customer_email"]


@pytest.mark.parametrize("case", BUNDLES, ids=[sku_id(c) for c in BUNDLES])
def test_bundle_webhook_queues_fulfill_bundle_task(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """Bundle SKUs queue fulfill_bundle_task with the bundle product_type."""
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    mocker.patch("app.workers.tasks.fulfill_bundle_task", fake_task)

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_task.delay.assert_called_once()
    kwargs = fake_task.delay.call_args.kwargs
    assert kwargs["product_type"] == case.product_type


@pytest.mark.parametrize(
    "case",
    [c for c in ONE_TIME if c.product_type in ("rfp_express", "rfp_complete")],
    ids=lambda c: c.product_type,
)
def test_rfp_webhook_with_brief_queues_rfp_task(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """RFP SKUs with an `rfp_description` go straight to fulfill_rfp_task."""
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    mocker.patch("app.workers.tasks.fulfill_rfp_task", fake_task)

    session = stripe_session_factory(
        case.product_type,
        rfp_description="Need cloud migration vendor for SG retail chain.",
    )
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_task.delay.assert_called_once()
    kwargs = fake_task.delay.call_args.kwargs
    assert kwargs["product_type"] == case.product_type
    assert kwargs["vendor_url"] == "https://example.test"


@pytest.mark.parametrize(
    "case",
    [c for c in ONE_TIME if c.product_type in ("rfp_express", "rfp_complete")],
    ids=lambda c: c.product_type,
)
def test_rfp_webhook_without_brief_defers_to_intake(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """No `rfp_description` → deferred intake."""
    fake_defer = AsyncMock(return_value=None)
    mocker.patch("app.api.stripe_webhook._defer_rfp_to_intake", fake_defer)

    session = stripe_session_factory(case.product_type)  # no rfp_description
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_defer.assert_awaited_once()


@pytest.mark.parametrize(
    "case",
    [c for c in ONE_TIME if c.product_type not in ("rfp_express", "rfp_complete")],
    ids=lambda c: c.product_type,
)
def test_standalone_one_time_calls_fulfill_standalone(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """vendor_proof / pdpa / notarization one-time SKUs use the standalone handler."""
    fake_standalone = AsyncMock(return_value=True)
    mocker.patch(
        "app.api.stripe_webhook._fulfill_standalone_no_report", fake_standalone
    )

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_standalone.assert_awaited_once()
    kwargs = fake_standalone.await_args.kwargs
    assert kwargs["product_type"] == case.product_type


def test_webhook_rejects_bad_signature(client, signed_webhook):
    """Tampered signature → 400. Requests `signed_webhook` so the fixture's
    monkeypatch ensures STRIPE_WEBHOOK_SECRET is set (otherwise the handler
    would 500 before even attempting signature verification)."""
    resp = client.post(
        "/api/v1/stripe/webhook",
        content=b'{"id":"evt_x","type":"checkout.session.completed","data":{"object":{}}}',
        headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
    )
    assert resp.status_code == 400
