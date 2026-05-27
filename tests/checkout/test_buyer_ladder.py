"""Buyer Plan redesign (2026-05) — regression tests for the new ladder.

These tests pin the four behavioural changes from the strategy diagnosis
(`DIAGNOSIS: Problems with the Current Buyer Plan`, Section 1.1):

  1. The new ladder has a sub-499 entry point (Starter at SGD 99).
  2. There is exactly ONE tier per price band — no duplicate 499s.
  3. Notarisations are NOT bundled into a buyer tier; they exist as the
     `notana_document_monthly` add-on.
  4. Buyer tiers are correctly classified as subscriptions in MODE_MAP and
     route through `_activate_subscription` in the webhook.

The catalog is also asserted against the canonical sources (pricing.py +
MODE_MAP + SUBSCRIPTION_PRODUCT_TYPES) so silent drift fails fast.
"""
from unittest.mock import AsyncMock

import pytest

from app.api.stripe_checkout import MODE_MAP, PROCUREMENT_PRODUCTS  # type: ignore
from app.api.stripe_webhook import SUBSCRIPTION_PRODUCT_TYPES
from app.services.pricing import PRODUCTS, get_product
from tests.fixtures.product_catalog import BUYER_LADDER, sku_id
from tests.fixtures.stripe_events import wrap_event


BUYER_TIER_KEYS = (
    "buyer_starter_monthly",
    "buyer_starter_annual",
    "buyer_pro_monthly",
    "buyer_pro_annual",
    "buyer_enterprise_monthly",
    "buyer_enterprise_annual",
)
NOTANA_KEY = "notana_document_monthly"


# ── 1. Catalog completeness — pricing.py + MODE_MAP + webhook agree ──────────


@pytest.mark.parametrize("key", (*BUYER_TIER_KEYS, NOTANA_KEY))
def test_buyer_sku_present_in_pricing_catalog(key):
    """Every new buyer SKU must be defined in app/services/pricing.py."""
    p = get_product(key)
    assert p is not None, f"{key} missing from PRODUCTS"
    assert p["type"] == "subscription"
    assert p["price_sgd"] > 0
    assert p["price_cents"] == p["price_sgd"] * 100


@pytest.mark.parametrize("key", (*BUYER_TIER_KEYS, NOTANA_KEY))
def test_buyer_sku_routes_as_subscription(key):
    """MODE_MAP and the webhook's subscription set must include the SKU."""
    assert MODE_MAP.get(key) == "subscription", f"{key} not subscription in MODE_MAP"
    assert key in SUBSCRIPTION_PRODUCT_TYPES, (
        f"{key} not in webhook SUBSCRIPTION_PRODUCT_TYPES — webhook will "
        f"fall through to bundle/standalone fulfillment"
    )


# ── 2. Pricing ladder shape — addresses diagnosis problems 1 & 2 ─────────────


def test_starter_is_below_legacy_499_floor():
    """Diagnosis Problem #2: no tier below 499/mo. Starter must fix this."""
    assert PRODUCTS["buyer_starter_monthly"]["price_sgd"] < 499


def test_ladder_is_strictly_increasing():
    """Each rung priced higher than the previous — kills the duplicate-499 issue."""
    ladder = [
        PRODUCTS["buyer_starter_monthly"]["price_sgd"],
        PRODUCTS["buyer_pro_monthly"]["price_sgd"],
        PRODUCTS["buyer_enterprise_monthly"]["price_sgd"],
    ]
    assert ladder == sorted(ladder), f"ladder not ascending: {ladder}"
    assert len(set(ladder)) == len(ladder), (
        f"Diagnosis Problem #1 regressed — duplicate prices in buyer ladder: {ladder}"
    )


@pytest.mark.parametrize(
    "monthly,annual",
    [
        ("buyer_starter_monthly", "buyer_starter_annual"),
        ("buyer_pro_monthly", "buyer_pro_annual"),
        ("buyer_enterprise_monthly", "buyer_enterprise_annual"),
    ],
)
def test_annual_is_two_months_free(monthly, annual):
    """Annual = 10 × monthly per the existing convention (vendor_active, pdpa_monitor)."""
    m = PRODUCTS[monthly]["price_sgd"]
    a = PRODUCTS[annual]["price_sgd"]
    assert a == m * 10, f"{annual} should be 10× {monthly} ({m * 10}), got {a}"


# ── 3. Notarisations are NOT bundled into buyer tiers — Diagnosis Problem #3 ─


@pytest.mark.parametrize("key", BUYER_TIER_KEYS)
def test_buyer_tier_description_does_not_advertise_notarisations(key):
    """Buyer tier copy must not claim N notarisations — that's the add-on's job."""
    description = PRODUCTS[key]["description"].lower()
    assert "notarization" not in description and "notarisation" not in description, (
        f"{key} description still advertises notarisations: {description!r}. "
        f"Notarisations belong to {NOTANA_KEY}, not buyer tiers."
    )


def test_notana_addon_is_its_own_sku():
    """Notana Document exists as a discrete subscription SKU, not bundled into a tier."""
    addon = get_product(NOTANA_KEY)
    assert addon is not None
    assert addon["type"] == "subscription"
    # Sanity: it should mention notarisation in its copy (it's the whole point).
    assert "notari" in addon["description"].lower()


# ── 4. Procurement-only gating extends to the new ladder ─────────────────────


@pytest.mark.parametrize("key", (*BUYER_TIER_KEYS, NOTANA_KEY))
def test_buyer_sku_is_procurement_gated(key):
    """A vendor account must not be able to buy any buyer-side plan."""
    assert key in PROCUREMENT_PRODUCTS, (
        f"{key} is not in PROCUREMENT_PRODUCTS — vendors could purchase it "
        f"and break the role distinction the redesign is supposed to enforce"
    )


# ── 5. Webhook routing — buyer SKUs hit _activate_subscription ───────────────


@pytest.mark.parametrize("case", BUYER_LADDER, ids=[sku_id(c) for c in BUYER_LADDER])
def test_buyer_webhook_activates_subscription(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """A `checkout.session.completed` event for a buyer SKU must call
    `_activate_subscription` with the right product_type — same dispatch
    path as every other subscription, no special-casing."""
    fake_activate = AsyncMock(return_value=None)
    mocker.patch("app.api.stripe_webhook._activate_subscription", fake_activate)

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))

    assert resp.status_code == 200
    fake_activate.assert_awaited_once()
    kwargs = fake_activate.await_args.kwargs
    assert kwargs["product_type"] == case.product_type
    assert kwargs["customer_email"] == session["customer_email"]
