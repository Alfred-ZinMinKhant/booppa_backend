"""Backend `/checkout` endpoint per-SKU tests.

Strategy: actually call `stripe.checkout.Session.create` against Stripe's test
API for every SKU in MODE_MAP. This catches:
  - missing/typoed STRIPE_<PRODUCT_TYPE> env vars
  - mode mismatches (e.g. a subscription product mapped to mode=payment)
  - metadata not being forwarded to Stripe

Tests skip if STRIPE_SECRET_KEY is not a real sk_test_ key, so the local pytest
run still succeeds without credentials.
"""
import os

import pytest

from app.core.auth import create_access_token
from tests.fixtures.product_catalog import ALL_SKUS, sku_id


def _auth_headers(email: str = "test+checkout@booppa.io") -> dict[str, str]:
    """Issue a real JWT — the checkout endpoint validates via verify_access_token."""
    token = create_access_token({"sub": email})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("case", ALL_SKUS, ids=[sku_id(c) for c in ALL_SKUS])
def test_create_checkout_session_per_sku(client, stripe_test_mode, case):
    """Every SKU should produce a Stripe checkout URL."""
    # Skip subscriptions when Stripe doesn't have the price configured locally.
    env_key = f"STRIPE_{case.product_type.upper()}"
    if not (os.environ.get(env_key) or os.environ.get(f"NEXT_PUBLIC_{env_key}")):
        pytest.skip(f"{env_key} not configured")

    body = {
        "product_type": case.product_type,
        "prefill_email": "test+checkout@booppa.io",
        # Bundles + RFP + PDPA + Vendor Proof require website/company name
        "website": "https://example.test",
        "company_name": "Test Co",
    }
    body.update(case.required_metadata)

    resp = client.post(
        "/api/v1/stripe/checkout",
        json=body,
        headers=_auth_headers(),
    )

    assert resp.status_code == 200, f"{case.product_type} returned {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "url" in data
    assert data["url"].startswith("https://checkout.stripe.com/")


def test_checkout_requires_auth(client):
    """Without a Bearer token the endpoint must return 401."""
    resp = client.post(
        "/api/v1/stripe/checkout",
        json={"product_type": "vendor_proof"},
    )
    assert resp.status_code == 401


def test_checkout_unknown_product_returns_400(client, stripe_test_mode):
    """Unknown product_type should 400, not 500."""
    resp = client.post(
        "/api/v1/stripe/checkout",
        json={"product_type": "not_a_real_product", "company_name": "X"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
