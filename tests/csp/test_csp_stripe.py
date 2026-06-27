"""CSP Stripe wiring + access-gate tests.

Covers the three CSP SKUs (csp_pack_monthly, csp_monitoring_monthly,
csp_pack_onetime) and the 402 access gate: the CSP router auto-provisions an
inactive org for any authenticated user, and only a paid Stripe purchase
(via the webhook) flips it to active.
"""
import uuid

import pytest

from tests.fixtures.stripe_events import wrap_event


def _seed_user(db, email: str):
    from app.core.models import User
    user = User(
        email=email,
        hashed_password="not-a-real-hash",  # NOT NULL
        role="VENDOR",
        plan="free",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_header(email: str) -> dict:
    from app.core.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


def _org_for(db, user_id):
    from app.core.models_csp import CspOrganisation
    return (
        db.query(CspOrganisation)
        .filter(CspOrganisation.owner_user_id == user_id)
        .first()
    )


# ── MODE_MAP ──────────────────────────────────────────────────────────────────

def test_mode_map_has_csp_skus():
    from app.api.stripe_checkout import MODE_MAP
    assert MODE_MAP["csp_pack_monthly"] == "subscription"
    assert MODE_MAP["csp_monitoring_monthly"] == "subscription"
    assert MODE_MAP["csp_pack_onetime"] == "payment"


# ── ACCESS GATE ───────────────────────────────────────────────────────────────

def test_csp_endpoint_blocked_when_org_inactive(client, test_db):
    email = f"csp-gate+{uuid.uuid4().hex[:8]}@booppa.io"
    _seed_user(test_db, email)

    r = client.get("/api/v1/csp/profile", headers=_auth_header(email))
    assert r.status_code == 402, r.text

    # The auth adapter still provisions an (inactive) org behind the gate.
    from app.core.models import User
    uid = test_db.query(User).filter(User.email == email).first().id
    org = _org_for(test_db, uid)
    assert org is not None
    assert (org.subscription_status or "inactive") == "inactive"


def test_pricing_open_without_subscription(client, test_db):
    # /pricing has no auth dependency and must stay reachable.
    r = client.get("/api/v1/csp/pricing")
    assert r.status_code == 200
    assert any(t.get("tier") == "full" for t in r.json())


# ── WEBHOOK FULFILLMENT ───────────────────────────────────────────────────────

def test_subscription_activates_csp_access(
    client, test_db, post_webhook, stripe_session_factory, email_capture
):
    email = f"csp-sub+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email)

    session = stripe_session_factory("csp_pack_monthly", customer_email=email)
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    org = _org_for(test_db, user.id)
    test_db.refresh(org)
    assert org.subscription_status == "active"
    assert org.plan == "csp"
    assert org.billing_type == "subscription"

    # CSP must NOT clobber the user's platform plan.
    test_db.refresh(user)
    assert user.plan == "free"

    # Gate now passes (profile not created yet → 404, not 402).
    r = client.get("/api/v1/csp/profile", headers=_auth_header(email))
    assert r.status_code in (200, 404), r.text


def test_onetime_grants_csp_access(
    client, test_db, post_webhook, stripe_session_factory, email_capture
):
    email = f"csp-1x+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email)

    session = stripe_session_factory("csp_pack_onetime", customer_email=email)
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    org = _org_for(test_db, user.id)
    test_db.refresh(org)
    assert org.subscription_status == "active"
    assert org.billing_type == "one_time"
    assert org.plan == "csp"


def test_email_mismatch_does_not_activate(
    client, test_db, post_webhook, stripe_session_factory, email_capture
):
    # The buyer's account email differs from the Stripe customer email → no row
    # matches, so nothing is activated (the alert path fires instead).
    account_email = f"csp-acct+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, account_email)

    session = stripe_session_factory(
        "csp_pack_monthly", customer_email="someone-else@booppa.io"
    )
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    org = _org_for(test_db, user.id)
    assert org is None  # never provisioned — buyer never resolved
