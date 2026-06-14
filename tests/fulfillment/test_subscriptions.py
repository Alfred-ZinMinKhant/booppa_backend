"""Subscription activation: User.plan set, Subscription row written, email sent."""
import pytest

from tests.fixtures.product_catalog import SUBSCRIPTIONS, sku_id
from tests.fixtures.stripe_events import wrap_event


def _seed_user(db, email: str):
    from app.core.models import User
    user = User(
        email=email,
        hashed_password="not-a-real-hash",  # User.hashed_password is NOT NULL
        role="VENDOR",
        plan="free",
        website="https://example.test",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.parametrize("case", SUBSCRIPTIONS, ids=[sku_id(c) for c in SUBSCRIPTIONS])
def test_subscription_activates_plan_and_emails(
    case, client, test_db, post_webhook, stripe_session_factory, email_capture, mocker
):
    """End-to-end through `_activate_subscription`:
    - User.plan is updated to the mapped plan
    - Subscription row inserted with status='active'
    - Activation email captured with the right label in the subject
    """
    # Avoid kicking off Celery side-effects we don't care about here.
    mocker.patch("app.workers.tasks.pdpa_monitor_monthly_rescan_task.delay", return_value=None)
    mocker.patch("app.api.stripe_webhook._apply_subscription_score_lever", return_value=None)
    # Score recalc + activity logging touch unrelated tables that may be empty
    mocker.patch("app.api.stripe_webhook._log_purchase_activity", return_value=None)

    email = f"sub+{case.product_type}@booppa.io"
    user = _seed_user(test_db, email)

    session = stripe_session_factory(case.product_type, customer_email=email)
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    # Re-fetch user
    test_db.refresh(user)
    assert user.plan != "free", f"plan not updated for {case.product_type}"
    assert user.stripe_subscription_id == session["subscription"]
    assert user.stripe_customer_id == session["customer"]

    # Subscription row
    from app.core.models import Subscription
    sub = test_db.query(Subscription).filter(
        Subscription.stripe_subscription_id == session["subscription"]
    ).first()
    assert sub is not None
    assert sub.status == "active"
    assert sub.product_type == case.product_type

    # Email — most tiers use the "Your <label> subscription is active" subject,
    # but suites + buyer tiers ship a richer itemised onboarding email whose
    # subject is "Welcome to <label> — here's everything included" (the
    # "subscription is now active" line lives in the body). Accept either as a
    # valid activation email.
    def _is_activation(m: dict) -> bool:
        subject = m["subject"].lower()
        return (
            "subscription is active" in subject
            or "here's everything included" in subject
        ) and "subscription is now active" in m["body"].lower()

    # vendor_active / vendor_pro no longer emit a synchronous activation email:
    # to avoid inbox spam, their single consolidated welcome digest (scores +
    # snapshot PDF + GeBIZ alerts + feature checklist) is delivered ASYNC by
    # vendor_active_health_check_task, queued via the first-cycle wrapper. That
    # email is asserted directly in
    # tests/test_vendor_snapshot.py::test_health_check_links_snapshot_pdf. Here
    # we've already verified plan activation + the Subscription row, which is the
    # synchronous contract for these tiers.
    ASYNC_WELCOME_TIERS = {
        "vendor_active_monthly", "vendor_active_annual",
        "vendor_pro_monthly", "vendor_pro_annual",
    }
    if case.product_type in ASYNC_WELCOME_TIERS:
        return

    assert any(_is_activation(m) and m["to"] == email for m in email_capture), \
        f"no activation email captured for {case.product_type}: {[m['subject'] for m in email_capture]}"


def test_subscription_idempotent_on_replay(
    client, test_db, post_webhook, stripe_session_factory, email_capture, mocker
):
    """Replaying the same webhook should not double-insert Subscription rows."""
    mocker.patch("app.api.stripe_webhook._apply_subscription_score_lever", return_value=None)
    mocker.patch("app.api.stripe_webhook._log_purchase_activity", return_value=None)

    email = "sub+replay@booppa.io"
    _seed_user(test_db, email)
    session = stripe_session_factory("vendor_active_monthly", customer_email=email)
    event = wrap_event(session, event_id="evt_replay_xyz")

    post_webhook(event)
    post_webhook(event)  # idempotency guard

    from app.core.models import Subscription
    rows = test_db.query(Subscription).filter(
        Subscription.stripe_subscription_id == session["subscription"]
    ).all()
    assert len(rows) == 1


def test_activation_email_sent_once_across_dual_webhook_delivery(
    client, test_db, post_webhook, stripe_session_factory, email_capture, mocker
):
    """A single subscription must yield exactly one activation email even when
    `_activate_subscription` is reached twice via DIFFERENT events.

    Stripe delivers both `checkout.session.completed` and
    `customer.subscription.created` for a new subscription (and may re-deliver
    either). Those carry different `event.id`s, so the event-level idempotency
    guard does NOT collapse them — only the once-per-subscription side-effect
    guard (keyed on stripe_subscription_id) does. Without it the buyer is
    double-emailed (the exact regression the forensic audit caught with the
    duplicate Standard Suite activation emails).
    """
    mocker.patch("app.api.stripe_webhook._apply_subscription_score_lever", return_value=None)
    mocker.patch("app.api.stripe_webhook._log_purchase_activity", return_value=None)

    email = "sub+dualdelivery@booppa.io"
    _seed_user(test_db, email)

    # Use a tier that emits a SYNCHRONOUS activation email (tender_intelligence),
    # so "exactly one" is a meaningful count. vendor_active/vendor_pro now deliver
    # their welcome async via the digest task, so the same once-per-subscription
    # guard instead collapses a double first-cycle queue rather than a double
    # email — both are governed by the identical `first_activation` SETNX gate.
    session = stripe_session_factory("tender_intelligence_monthly", customer_email=email)
    # Same session (same `subscription` id) wrapped in two distinct events so the
    # processed-event idempotency check can't short-circuit the second.
    post_webhook(wrap_event(session, event_id="evt_dual_a"))
    post_webhook(wrap_event(session, event_id="evt_dual_b"))

    activation_emails = [
        m for m in email_capture
        if m["to"] == email and "subscription is active" in m["subject"].lower()
    ]
    assert len(activation_emails) == 1, \
        f"expected exactly one activation email, got {len(activation_emails)}: " \
        f"{[m['subject'] for m in activation_emails]}"
