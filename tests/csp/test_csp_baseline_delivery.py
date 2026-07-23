"""CSP Day-1 deliverable: checkout entity capture → baseline task → serve-back.

Regression guard for the gap where a SGD 3,999 `csp_pack_onetime` buyer received
a two-line activation email with nothing attached. The 8 AML/CFT documents still
correctly wait for a CSP profile; what these tests pin is that *something real*
ships on day one, from BOTH purchase paths through the single shared helper.
"""
import asyncio
import uuid

import pytest

from tests.fixtures.stripe_events import wrap_event


def _seed_user(db, email: str, **kw):
    from app.core.models import User
    user = User(
        email=email,
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="free",
        is_active=True,
        **kw,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_header(email: str) -> dict:
    from app.core.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


@pytest.fixture
def queued(monkeypatch):
    """Capture csp.run_baseline dispatches instead of hitting the broker."""
    calls = []

    class _Stub:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append(kwargs.get("kwargs") or {})

    import app.workers.csp_tasks as csp_tasks
    monkeypatch.setattr(csp_tasks, "run_csp_baseline_for_user", _Stub)
    return calls


# ── ENTITY CAPTURE AT CHECKOUT ────────────────────────────────────────────────

CSP_SKUS = ["csp_pack_onetime", "csp_pack_monthly", "csp_monitoring_monthly"]


@pytest.mark.parametrize("sku", CSP_SKUS)
def test_csp_checkout_422s_without_company_or_website(client, test_db, sku):
    """Without an entity we cannot run the ACRA lookup the baseline is built on,
    so the buyer must not be allowed to pay first and find out after."""
    email = f"csp-gate+{uuid.uuid4().hex[:8]}@booppa.io"
    _seed_user(test_db, email)

    r = client.post(
        "/api/v1/stripe/checkout",
        json={"productType": sku},
        headers=_auth_header(email),
    )
    assert r.status_code == 422, r.text
    assert "company name" in r.json()["detail"].lower()

    # Company alone is still not enough — the website is captured too.
    r = client.post(
        "/api/v1/stripe/checkout",
        json={"productType": sku, "company_name": "Acme Corporate Services Pte Ltd"},
        headers=_auth_header(email),
    )
    assert r.status_code == 422, r.text
    assert "website" in r.json()["detail"].lower()


def test_csp_checkout_blocks_struck_off_entity_before_payment(
    client, test_db, monkeypatch
):
    """A struck-off entity must be stopped BEFORE the charge — the webhook can
    only warn once the money has moved."""
    email = f"csp-struck+{uuid.uuid4().hex[:8]}@booppa.io"
    _seed_user(test_db, email)

    async def _fake(uen=None, company_name=None):
        return {
            "found": True, "live": False, "uen": "201912345A",
            "entity_status": "Struck Off",
        }
    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _fake)

    r = client.post(
        "/api/v1/stripe/checkout",
        json={
            "productType": "csp_pack_onetime",
            "company_name": "Defunct Services Pte Ltd",
            "website": "https://defunct.example",
        },
        headers=_auth_header(email),
    )
    assert r.status_code == 409, r.text
    assert "Struck Off" in r.json()["detail"]


def test_csp_checkout_persists_entity_to_profile(client, test_db, monkeypatch):
    """Supplied company/website are back-filled onto the user so the baseline
    task and the later CSP profile form both see them."""
    from app.core.models import User

    email = f"csp-persist+{uuid.uuid4().hex[:8]}@booppa.io"
    _seed_user(test_db, email)

    async def _fake(uen=None, company_name=None):
        return {
            "found": True, "live": True, "uen": "201912345A",
            "entity_status": "Live", "registered_name": "ACME CORPORATE SERVICES PTE. LTD.",
        }
    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _fake)

    # Stripe itself is unconfigured in tests; the entity block runs before the
    # session is created, so a downstream failure is fine — we assert the writes.
    client.post(
        "/api/v1/stripe/checkout",
        json={
            "productType": "csp_pack_monthly",
            "company_name": "Acme Corporate Services Pte Ltd",
            "website": "https://acme.example",
        },
        headers=_auth_header(email),
    )

    test_db.expire_all()
    user = test_db.query(User).filter(User.email == email).first()
    assert user.company == "Acme Corporate Services Pte Ltd"
    assert user.website == "https://acme.example"
    assert user.uen == "201912345A"


# ── ONE FIX, TWO CALL SITES ───────────────────────────────────────────────────

def test_onetime_purchase_queues_baseline(
    test_db, post_webhook, stripe_session_factory, email_capture, queued
):
    email = f"csp-1x-base+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email, company="Acme CS Pte Ltd", website="https://acme.example")

    session = stripe_session_factory("csp_pack_onetime", customer_email=email)
    assert post_webhook(wrap_event(session)).status_code == 200

    assert len(queued) == 1, "one-time pack must queue the Day-1 baseline"
    assert queued[0]["user_id"] == str(user.id)
    assert queued[0]["billing_type"] == "one_time"
    assert queued[0]["plan"] == "csp"


def test_monthly_subscription_queues_baseline(
    test_db, post_webhook, stripe_session_factory, email_capture, queued
):
    """Gianpaolo's 'one fix, not two': the monthly path hits the same shared
    helper, so it gets the artifact without a second patch."""
    email = f"csp-sub-base+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email, company="Acme CS Pte Ltd", website="https://acme.example")

    session = stripe_session_factory("csp_pack_monthly", customer_email=email)
    assert post_webhook(wrap_event(session)).status_code == 200

    assert len(queued) == 1, "monthly subscription must queue the Day-1 baseline"
    assert queued[0]["user_id"] == str(user.id)
    assert queued[0]["billing_type"] == "subscription"


def test_activation_no_longer_sends_the_bare_email(
    test_db, post_webhook, stripe_session_factory, email_capture, queued
):
    """The two-line 'is active' email with nothing attached must be gone. The one
    email the buyer gets now comes from the baseline task and carries the PDF."""
    email = f"csp-noemail+{uuid.uuid4().hex[:8]}@booppa.io"
    _seed_user(test_db, email)

    session = stripe_session_factory("csp_pack_onetime", customer_email=email)
    assert post_webhook(wrap_event(session)).status_code == 200

    subjects = [
        (m.get("subject") or "") for m in email_capture
    ] if email_capture else []
    assert not any("CSP Compliance Pack is active" in s for s in subjects), subjects


def test_helper_is_async(monkeypatch):
    """Must stay `async def`: both call sites already run under asyncio.run() in
    the Celery worker, where a sync helper bridging via asyncio.run() no-ops."""
    import inspect
    from app.services.csp_access import deliver_csp_activation
    assert inspect.iscoroutinefunction(deliver_csp_activation)


def test_queue_failure_does_not_undo_paid_activation(test_db, monkeypatch):
    """Access is already committed — a broker outage must alert, not roll back."""
    from app.services.csp_access import deliver_csp_activation
    import app.workers.csp_tasks as csp_tasks

    email = f"csp-brokerdown+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email)

    class _Boom:
        @staticmethod
        def apply_async(*a, **k):
            raise RuntimeError("broker unreachable")

    alerts = []

    async def _alert(**kw):
        alerts.append(kw)

    monkeypatch.setattr(csp_tasks, "run_csp_baseline_for_user", _Boom)
    monkeypatch.setattr(
        "app.services.fulfillment.helpers._alert_payment_fulfillment_issue", _alert
    )

    org = asyncio.run(deliver_csp_activation(
        test_db, user=user, plan="csp", billing_type="one_time"
    ))
    assert org.subscription_status == "active"
    assert alerts, "a failed baseline queue must raise a fulfillment alert"


# ── SERVE-BACK ────────────────────────────────────────────────────────────────

def test_baseline_latest_reports_unavailable_before_generation(client, test_db):
    from app.services.csp_access import activate_csp_access

    email = f"csp-nobase+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email)
    activate_csp_access(test_db, user=user, plan="csp", billing_type="one_time")

    r = client.get("/api/v1/csp/baseline/latest", headers=_auth_header(email))
    assert r.status_code == 200, r.text
    assert r.json() == {"available": False}


def test_baseline_latest_serves_persisted_report(client, test_db, monkeypatch):
    from datetime import datetime, timezone
    from app.core.models import Report
    from app.services.csp_access import activate_csp_access

    email = f"csp-base+{uuid.uuid4().hex[:8]}@booppa.io"
    user = _seed_user(test_db, email)
    activate_csp_access(test_db, user=user, plan="csp", billing_type="one_time")

    test_db.add(Report(
        owner_id=user.id,
        framework="csp_baseline",
        company_name="ACME CORPORATE SERVICES PTE. LTD.",
        assessment_data={
            "plan_label": "CSP Compliance Pack — Full",
            "billing_label": "One-time purchase",
            "s3_url": "https://stored.example/old.pdf",
            "s3_key": f"reports/csp-baseline-{user.id}.pdf",
            "acra_found": True,
            "uen": "201912345A",
        },
        status="completed",
        s3_url="https://stored.example/old.pdf",
        file_key=f"reports/csp-baseline-{user.id}.pdf",
        completed_at=datetime.now(timezone.utc),
    ))
    test_db.commit()

    # Presigns expire in 7 days, so the endpoint must re-presign rather than
    # hand back the stored URL.
    class _S3:
        bucket = "booppa-reports"

        class s3_client:
            @staticmethod
            def generate_presigned_url(*a, **k):
                return "https://fresh.example/new.pdf"

    monkeypatch.setattr("app.services.storage.S3Service", lambda *a, **k: _S3())

    r = client.get("/api/v1/csp/baseline/latest", headers=_auth_header(email))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["download_url"] == "https://fresh.example/new.pdf"
    assert body["acra_verified"] is True
    assert body["uen"] == "201912345A"
    # The ACRA legal name, never a raw domain.
    assert body["company_name"] == "ACME CORPORATE SERVICES PTE. LTD."
