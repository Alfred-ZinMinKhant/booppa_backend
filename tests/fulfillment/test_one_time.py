"""One-time product fulfillment: ensure the correct standalone handler is hit
and that downstream Celery tasks are queued for PDPA / Vendor Proof / Notarization.
"""
import pytest

from tests.fixtures.product_catalog import ONE_TIME, sku_id
from tests.fixtures.stripe_events import wrap_event


@pytest.mark.parametrize(
    "case",
    [c for c in ONE_TIME if c.product_type not in ("rfp_express", "rfp_complete")],
    ids=lambda c: c.product_type,
)
def test_one_time_standalone_handler_called(
    case, client, test_db, post_webhook, stripe_session_factory, mocker
):
    """vendor_proof / pdpa_quick_scan / compliance_notarization_* all flow through
    `_fulfill_standalone_no_report` when no report_id is on the metadata."""
    fake_handler = mocker.patch(
        "app.api.stripe_webhook._fulfill_standalone_no_report",
        new=mocker.AsyncMock(return_value=True),
    )

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    fake_handler.assert_awaited_once()
    kwargs = fake_handler.await_args.kwargs
    assert kwargs["product_type"] == case.product_type
    assert kwargs["customer_email"] == session["customer_email"]


def test_rfp_express_with_brief_queues_fulfill_rfp(
    client, post_webhook, stripe_session_factory, mocker
):
    fake_task = mocker.patch("app.workers.tasks.fulfill_rfp_task")
    fake_task.delay = mocker.MagicMock()

    session = stripe_session_factory(
        "rfp_express",
        rfp_description="Cloud migration for SG retail",
    )
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200
    fake_task.delay.assert_called_once()
    kwargs = fake_task.delay.call_args.kwargs
    assert kwargs["product_type"] == "rfp_express"
    assert kwargs["rfp_description"] == "Cloud migration for SG retail"


def test_rfp_complete_without_brief_defers(
    client, post_webhook, stripe_session_factory, mocker
):
    fake_defer = mocker.patch(
        "app.api.stripe_webhook._defer_rfp_to_intake",
        new=mocker.AsyncMock(return_value=None),
    )

    session = stripe_session_factory("rfp_complete")  # no rfp_description
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200
    fake_defer.assert_awaited_once()
    kwargs = fake_defer.await_args.kwargs
    assert kwargs["rfp_product_type"] == "rfp_complete"
