"""The price charged must match the product the metadata claims.

Entitlement is granted from `metadata.product_type`, which is set by whoever
created the Checkout Session. Until 2026-08-08 nothing compared that claim
against what was actually paid (AUDIT_2026-08-08.md P0-A/P0-B), so a session
created with a cheap price and expensive metadata bought the expensive product.

These tests pin the three branches of `_price_matches_product`, including the
two deliberate fail-open cases — those exist so a Stripe outage or a retired SKU
cannot strand a customer who has already been charged, and it matters that they
stay *narrow*.
"""
import pytest

from app.api.stripe_webhook import _price_matches_product
from tests.fixtures.stripe_events import wrap_event


CHEAP = "price_cheap_decoy"
REAL = "price_pro_suite_real"


@pytest.fixture
def priced(monkeypatch):
    """Configure a known Stripe price for pro_suite_monthly."""
    monkeypatch.setenv("STRIPE_PRO_SUITE_MONTHLY", REAL)
    return REAL


def _session(price_id: str | None, product_type: str = "pro_suite_monthly") -> dict:
    session: dict = {
        "id": "cs_test_binding",
        "metadata": {"product_type": product_type},
    }
    if price_id is not None:
        session["line_items"] = {"data": [{"price": {"id": price_id}}]}
    return session


# ── the check itself ──────────────────────────────────────────────────────────

def test_matching_price_passes(priced):
    ok, reason = _price_matches_product(_session(REAL), "pro_suite_monthly")
    assert ok, reason


def test_substituted_cheap_price_is_rejected(priced):
    """The P0-A attack: pay the cheapest SKU, claim the most expensive one."""
    ok, reason = _price_matches_product(_session(CHEAP), "pro_suite_monthly")
    assert not ok
    assert REAL in reason and CHEAP in reason


def test_unconfigured_product_fails_open(monkeypatch):
    """A retired SKU whose env var is gone must not block a paid customer."""
    monkeypatch.delenv("STRIPE_PRO_SUITE_MONTHLY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_STRIPE_PRO_SUITE_MONTHLY", raising=False)
    ok, reason = _price_matches_product(_session(CHEAP), "pro_suite_monthly")
    assert ok
    assert "cannot verify" in reason


def test_stripe_lookup_failure_fails_open(priced, mocker):
    """A Stripe outage must not strand fulfilment — but must say so."""
    mocker.patch(
        "stripe.checkout.Session.retrieve", side_effect=RuntimeError("stripe down")
    )
    ok, reason = _price_matches_product(_session(None), "pro_suite_monthly")
    assert ok
    assert "not blocking" in reason


def test_line_items_are_fetched_when_absent(priced, mocker):
    """Stripe does not send line items on the event; they must be retrieved."""
    fetch = mocker.patch(
        "stripe.checkout.Session.retrieve",
        return_value={"line_items": {"data": [{"price": {"id": REAL}}]}},
    )
    ok, reason = _price_matches_product(_session(None), "pro_suite_monthly")
    assert ok, reason
    fetch.assert_called_once()
    assert fetch.call_args.kwargs["expand"] == ["line_items"]


# ── end to end through the webhook ────────────────────────────────────────────

def test_mismatched_price_grants_no_entitlement(
    priced, client, post_webhook, stripe_session_factory, mocker
):
    """The whole point: a mismatch must reach no fulfilment branch at all."""
    activate = mocker.patch(
        "app.api.stripe_webhook._activate_subscription", mocker.AsyncMock()
    )
    alert = mocker.patch(
        "app.api.stripe_webhook._alert_payment_fulfillment_issue", mocker.AsyncMock()
    )

    session = stripe_session_factory("pro_suite_monthly")
    session["line_items"] = {"data": [{"price": {"id": CHEAP}}]}
    resp = post_webhook(wrap_event(session))

    # 200 so Stripe stops retrying — a retry cannot change the outcome.
    assert resp.status_code == 200
    assert resp.json()["fulfilled"] is False
    activate.assert_not_awaited()
    alert.assert_awaited_once()


def test_matching_price_still_fulfils(
    priced, client, post_webhook, stripe_session_factory, mocker
):
    """The guard must not break the ordinary paying customer."""
    activate = mocker.patch(
        "app.api.stripe_webhook._activate_subscription", mocker.AsyncMock()
    )

    session = stripe_session_factory("pro_suite_monthly")
    session["line_items"] = {"data": [{"price": {"id": REAL}}]}
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    activate.assert_awaited_once()
