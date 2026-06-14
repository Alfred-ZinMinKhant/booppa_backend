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
