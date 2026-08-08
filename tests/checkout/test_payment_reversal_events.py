"""Failed renewals and chargebacks.

Neither event had a handler before 2026-08-08 (AUDIT_2026-08-08.md P1-3): a
declined renewal was silent until the downgrade landed weeks later, and a
chargeback took the money back while the customer kept the deliverable.

The line these tests defend is *what a reversal revokes*: a dispute revokes
report access, a refund we issue does not — because the common refund is us
refunding an unscannable site, where the buyer is still owed the partial
document (`unscannable_scan_refund_invariant`).
"""
import json
import uuid
from types import SimpleNamespace

import pytest

from app.api.reports import _may_read_report


def _event(etype: str, obj: dict) -> dict:
    return {"id": f"evt_{uuid.uuid4().hex[:16]}", "type": etype, "data": {"object": obj}}


# ── what a reversal revokes ───────────────────────────────────────────────────

def test_chargeback_revokes_report_access():
    rep = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=None,  # even a public free scan is closed by a chargeback
        assessment_data={"access_revoked": True, "access_revoked_reason": "chargeback"},
    )
    assert not _may_read_report(rep, None)


def test_our_own_refund_does_not_revoke_access():
    """An unscannable-site refund still owes the buyer the document we produced."""
    uid = uuid.uuid4()
    rep = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uid,
        assessment_data={"refunded": True, "refund_source": "stripe_dashboard"},
    )
    assert _may_read_report(rep, SimpleNamespace(id=uid))


# ── dunning ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "next_attempt,expect_final", [(1893456000, False), (None, True)]
)
def test_payment_failed_emails_the_customer(
    client, post_webhook, mocker, next_attempt, expect_final
):
    send = mocker.patch(
        "app.services.email_service.EmailService.send_html_email",
        mocker.AsyncMock(return_value=True),
    )
    alert = mocker.patch(
        "app.api.stripe_webhook._alert_payment_fulfillment_issue", mocker.AsyncMock()
    )

    resp = post_webhook(
        _event(
            "invoice.payment_failed",
            {
                "id": "in_test_dunning",
                "customer_email": "buyer@example.com",
                "amount_due": 45000,
                "next_payment_attempt": next_attempt,
                "hosted_invoice_url": "https://invoice.stripe.com/x",
            },
        )
    )

    assert resp.status_code == 200
    send.assert_awaited_once()
    body = send.await_args.kwargs["body_html"]
    assert "invoice.stripe.com/x" in body
    # Only the last attempt escalates to a human.
    assert alert.await_count == (1 if expect_final else 0)
    # A retry still pending must not claim access is ending.
    if not expect_final:
        assert "free tier" not in body


def test_payment_failed_does_not_downgrade_the_plan(client, post_webhook, mocker):
    """Stripe retries for ~2 weeks; only `customer.subscription.*` downgrades."""
    mocker.patch(
        "app.services.email_service.EmailService.send_html_email",
        mocker.AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.api.stripe_webhook._alert_payment_fulfillment_issue", mocker.AsyncMock()
    )
    activate = mocker.patch(
        "app.api.stripe_webhook._activate_subscription", mocker.AsyncMock()
    )

    post_webhook(
        _event(
            "invoice.payment_failed",
            {"id": "in_x", "customer_email": "buyer@example.com", "next_payment_attempt": None},
        )
    )
    activate.assert_not_awaited()


def test_rejected_dunning_email_is_logged_not_swallowed(client, post_webhook, mocker, caplog):
    """`send_html_email` returns False rather than raising — check the return."""
    mocker.patch(
        "app.services.email_service.EmailService.send_html_email",
        mocker.AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.api.stripe_webhook._alert_payment_fulfillment_issue", mocker.AsyncMock()
    )
    with caplog.at_level("ERROR"):
        post_webhook(
            _event(
                "invoice.payment_failed",
                {"id": "in_y", "customer_email": "buyer@example.com",
                 "next_payment_attempt": 1893456000},
            )
        )
    assert "customer was not warned" in caplog.text


# ── disputes ──────────────────────────────────────────────────────────────────

def test_dispute_alerts_even_when_no_session_matches(client, post_webhook, mocker):
    """A dispute we cannot trace to a purchase is the case most needing a human."""
    alert = mocker.patch(
        "app.api.stripe_webhook._alert_payment_fulfillment_issue", mocker.AsyncMock()
    )
    mocker.patch("stripe.Charge.retrieve", return_value={"payment_intent": None})

    resp = post_webhook(
        _event(
            "charge.dispute.created",
            {"id": "dp_1", "charge": "ch_1", "amount": 29900, "reason": "fraudulent",
             "status": "needs_response"},
        )
    )
    assert resp.status_code == 200
    alert.assert_awaited_once()
    assert alert.await_args.kwargs["extra"]["dispute_id"] == "dp_1"
