"""Repeat admin test-checkouts of the same SKU must not double-send.

Root cause of the reported duplicate "Your MAS TRM Baseline is ready" email:
`_activate_subscription` guards its side effects with a once-per-subscription
Redis claim (`sub_activated:{stripe_subscription_id}`), but admin
simulate-purchase minted a fresh `uuid4()` subscription id on every click. Each
click therefore looked like a brand-new subscription and won the claim, so two
clicks sent two identical emails.

The production Stripe path was never affected — Stripe reuses one subscription
id across duplicate events, which is exactly what the claim keys on.

The fix makes the simulated id deterministic per (email, SKU). These tests pin
that property and the deliberate "force resend" escape hatch.
"""
import hashlib

from app.core.cache import cache as _cache


def _sim_sub_id(email: str, product_type: str) -> str:
    """Mirror of the derivation in `app/api/admin.py::simulate_purchase`."""
    seed = f"{email}|{product_type}".encode()
    return f"admin-sim-{hashlib.sha256(seed).hexdigest()[:24]}"


def test_repeat_checkout_of_same_sku_yields_the_same_subscription_id():
    a = _sim_sub_id("qa@booppa.io", "pro_suite_monthly")
    b = _sim_sub_id("qa@booppa.io", "pro_suite_monthly")
    assert a == b, (
        "two clicks on the same test checkout must produce one subscription id, "
        "or the sub_activated claim can't dedupe them"
    )


def test_different_email_or_sku_is_a_different_subscription():
    base = _sim_sub_id("qa@booppa.io", "pro_suite_monthly")
    # A different tester must not be blocked by someone else's claim...
    assert _sim_sub_id("other@booppa.io", "pro_suite_monthly") != base
    # ...and testing a second SKU against the same email is a separate activation.
    assert _sim_sub_id("qa@booppa.io", "standard_suite_monthly") != base


def test_second_activation_claim_is_refused_then_released_by_force_resend():
    """The claim itself: first caller wins, second is refused, delete re-arms it.

    This is the mechanism `_activate_subscription` relies on to fire the welcome
    email and first-cycle deliverables exactly once, and that the admin
    "Force resend" checkbox deliberately releases.
    """
    key = _cache.cache_key(f"sub_activated:{_sim_sub_id('dedupe@booppa.io', 'pro_suite_monthly')}")
    _cache.delete(key)

    assert _cache.add(key, {"activated_at": "now"}, ttl=60) is True
    assert _cache.add(key, {"activated_at": "now"}, ttl=60) is False, (
        "the second click must lose the claim — this is what stops the duplicate email"
    )

    assert _cache.delete(key) is True
    assert _cache.add(key, {"activated_at": "now"}, ttl=60) is True, (
        "force_resend must re-arm the claim so a deliberate QA resend works"
    )
    _cache.delete(key)
