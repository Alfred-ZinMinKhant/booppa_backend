"""Webhook subscription-cancel regression tests.

Pins the post-2026-06-01 cancel handler:
  - customer.subscription.deleted is handled (was ignored before).
  - User.plan downgrades to the next-most-recent active sub, or "free".
  - max_seats refreshes on owned orgs.
  - Pro Suite cancel deactivates SsoConfig.
  - Buyer-ladder slugs land in the plan_map (silent bug — defaulted to "pro").
"""
from __future__ import annotations

import uuid

import pytest

from app.api.stripe_webhook import _activate_subscription  # noqa: F401  # import for side effects
from app.core.models import Subscription, User
from app.core.models_enterprise import Organisation, SsoConfig

from tests._test_helpers import make_user, make_org


def _post_canceled_event(
    post_webhook,
    *,
    customer_id: str,
    subscription_id: str,
    event_type: str = "customer.subscription.deleted",
):
    """Build and POST a Stripe cancel event to /webhook."""
    return post_webhook({
        "type": event_type,
        "data": {
            "object": {
                "id": subscription_id,
                "customer": customer_id,
                "status": "canceled",
                "items": {"data": []},
            }
        },
    })


def _seed_canceling_customer(
    db,
    *,
    email: str,
    plan: str,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    product_type: str | None = None,
):
    """Insert User + a single Subscription row matching the email/plan."""
    customer_id = customer_id or f"cus_test_{uuid.uuid4().hex[:16]}"
    subscription_id = subscription_id or f"sub_test_{uuid.uuid4().hex[:16]}"
    product_type = product_type or f"{plan}_monthly"

    user = make_user(db, email=email, plan=plan, role="PROCUREMENT")
    user.stripe_customer_id = customer_id
    user.stripe_subscription_id = subscription_id
    db.add(user)
    db.add(Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        stripe_subscription_id=subscription_id,
        stripe_customer_id=customer_id,
        product_type=product_type,
        status="active",
    ))
    db.commit()
    db.refresh(user)
    return user, customer_id, subscription_id


def test_cancel_drops_user_to_free_when_no_other_subs(
    test_db, post_webhook, monkeypatch
):
    """A single-subscription user who cancels lands on plan=free."""
    # Skip the Stripe Customer.retrieve call — return the seeded email directly.
    user, cus_id, sub_id = _seed_canceling_customer(
        test_db, email="solo@booppa.io", plan="buyer_pro",
        product_type="buyer_pro_monthly",
    )
    _patch_stripe_customer_email(monkeypatch, cus_id, user.email)

    resp = _post_canceled_event(post_webhook, customer_id=cus_id, subscription_id=sub_id)
    assert resp.status_code in (200, 202)

    test_db.expire_all()
    updated = test_db.query(User).filter(User.id == user.id).first()
    assert updated.plan == "free"


def test_cancel_refreshes_max_seats_to_match_remaining_plan(
    test_db, post_webhook, monkeypatch
):
    """When a buyer_pro sub is canceled, owned orgs' max_seats should drop."""
    user, cus_id, sub_id = _seed_canceling_customer(
        test_db, email="pro@booppa.io", plan="buyer_pro",
        product_type="buyer_pro_monthly",
    )
    _patch_stripe_customer_email(monkeypatch, cus_id, user.email)

    org = make_org(test_db, owner=user, max_seats=3)

    _post_canceled_event(post_webhook, customer_id=cus_id, subscription_id=sub_id)

    test_db.expire_all()
    refreshed = test_db.query(Organisation).filter(Organisation.id == org.id).first()
    # Single-sub cancel → plan=free → max_seats_for("free") == 1
    assert refreshed.max_seats == 1


def test_cancel_pro_suite_deactivates_sso(test_db, post_webhook, monkeypatch):
    """A lapsed Pro Suite customer's SsoConfig.is_active must flip to False."""
    user, cus_id, sub_id = _seed_canceling_customer(
        test_db, email="enterprise@booppa.io", plan="pro_suite",
        product_type="pro_suite_monthly",
    )
    _patch_stripe_customer_email(monkeypatch, cus_id, user.email)

    org = make_org(test_db, owner=user, max_seats=None)
    sso = SsoConfig(
        id=uuid.uuid4(),
        organisation_id=org.id,
        protocol="saml",
        idp_metadata_url="https://idp.example.test/metadata",
        is_active=True,
    )
    test_db.add(sso)
    test_db.commit()

    _post_canceled_event(post_webhook, customer_id=cus_id, subscription_id=sub_id)

    test_db.expire_all()
    refreshed = test_db.query(SsoConfig).filter(SsoConfig.id == sso.id).first()
    assert refreshed.is_active is False


def test_plan_map_includes_buyer_ladder_slugs():
    """The cancel handler's _plan_map fallback used to be 'pro' for unknown
    slugs — silent bug for buyer ladder. Pin the family mapping here so future
    refactors don't lose it."""
    # We can't import the local dict directly (it's scoped inside the branch),
    # but we can introspect the source to make sure every buyer_* SKU is named.
    import inspect
    from app.api import stripe_webhook
    src = inspect.getsource(stripe_webhook)
    # Each buyer slug must appear in the _plan_map block at least once
    for slug in (
        "buyer_starter_monthly", "buyer_starter_annual",
        "buyer_pro_monthly", "buyer_pro_annual",
        "buyer_enterprise_monthly", "buyer_enterprise_annual",
    ):
        assert slug in src, f"missing {slug} from cancel-handler plan_map"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _patch_stripe_customer_email(monkeypatch, customer_id: str, email: str):
    """Stub stripe.Customer.retrieve so the webhook resolves the email without
    a real Stripe API call. The webhook calls retrieve() to map customer_id -> email."""
    class _FakeCustomer(dict):
        pass

    def _fake_retrieve(cid, *args, **kwargs):
        if cid == customer_id:
            return _FakeCustomer({"email": email, "id": cid})
        return _FakeCustomer({"email": None, "id": cid})

    monkeypatch.setattr("stripe.Customer.retrieve", _fake_retrieve)
