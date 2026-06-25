"""Test fixtures for booppa_backend.

Extends the original conftest with:
  - env-driven TEST_DATABASE_URL
  - moto-backed S3 + SES
  - EmailService capture (since Resend is the default path)
  - Stripe session factory + locally-signed webhook payload helper
  - stripe_test_mode skip-guard for tests that need a real Stripe test key
"""
import json
import os
import time
import uuid
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base, get_db
from app.core.config import settings
from app.main import app

# ── Database ────────────────────────────────────────────────────────────────

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://booppa:password@localhost:5432/booppa_test",
)

engine = create_engine(TEST_DATABASE_URL)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Tables the tests write to. Truncated on teardown so re-runs are clean.
# Other tables (marketplace_vendors seed data, gebiz_tenders, etc.) are left
# alone. List exists separately from Base.metadata because we deliberately do
# NOT use Base.metadata.create_all — see comment in `test_db`.
_TRUNCATABLE_TABLES = [
    "pending_rfp_intakes",
    "ropa_activities",
    "pdpa_self_declarations",
    "trm_evidence",
    "trm_controls",
    "organisation_members",
    "organisations",
    "vendor_scores",
    "vendor_status_snapshots",
    "subscriptions",
    "processed_webhook_events",
    "activity_logs",
    "funnel_events",
    "search_impressions",
    "verify_records",
    "certificate_logs",
    "reports",
    "users",
]


def _truncate_test_tables():
    """Wipe data the tests write to. CASCADE handles FK chains."""
    with engine.begin() as conn:
        for table in _TRUNCATABLE_TABLES:
            try:
                conn.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))
            except Exception:
                # Table may not exist in older alembic revs; that's fine.
                pass


@pytest.fixture(scope="function")
def test_db():
    """Test session against the alembic-migrated schema.

    We deliberately do NOT call `Base.metadata.create_all` — at least one
    model (RfpRequirementFlag in models_v8.py) declares the same index twice
    (once via `index=True` on the column, once via an explicit `Index(...)` in
    `__table_args__`), which raises DuplicateTable on `create_all`. Alembic
    sidesteps this because its migrations are hand-written.

    Setup expectation: `alembic upgrade head` has been run before pytest.
    The CI workflow already does this; locally, run it once after spinning up
    the test DB. See TESTING.md.
    """
    _truncate_test_tables()
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        _truncate_test_tables()


@pytest.fixture(scope="function")
def client(test_db, _disable_rate_limit):
    """TestClient with the test DB wired into get_db."""
    def override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def _disable_rate_limit(monkeypatch):
    """slowapi limits requests by remote IP. In tests every request comes from
    the same TestClient host, so the 20/minute /checkout limit trips quickly
    when parametrized tests run together. Disable rate-limiting for tests."""
    try:
        from app.api.stripe_checkout import _limiter
        monkeypatch.setattr(_limiter, "enabled", False)
    except Exception:
        pass


# ── AWS / S3 ────────────────────────────────────────────────────────────────
#
# Two paths:
#   - `s3_bucket`  — moto fake. Default. Fast, hermetic, no AWS creds needed.
#   - `real_s3`    — boto3 against the real `booppa-reports` bucket scoped to a
#                    unique `test/<run-id>/` prefix; cleans up on teardown.
#
# The autouse `_aws_credentials` fixture defaults to fake creds so a misconfigured
# test cannot reach real AWS by accident. Opting into `real_s3` overrides this.

@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch, request):
    """Force fake AWS creds — moto needs them, and they block real AWS access
    for any test that doesn't explicitly request `real_s3`."""
    if "real_s3" in request.fixturenames:
        return  # let the real fixture set real creds
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")


@pytest.fixture
def s3_bucket():
    """Moto-backed S3 with the configured bucket pre-created. Default choice
    for unit tests — no AWS credentials needed."""
    from moto import mock_aws
    import boto3

    with mock_aws():
        client = boto3.client("s3", region_name=settings.AWS_REGION)
        client.create_bucket(
            Bucket=settings.S3_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": settings.AWS_REGION},
        )
        yield client


@pytest.fixture
def real_s3(monkeypatch):
    """Real S3 against `booppa-reports` under a unique `test/<run-id>/` prefix.

    Cleanup: every key uploaded during the test is deleted on teardown. As
    defense-in-depth, the bucket has a lifecycle rule that expires anything
    under `test/` after 1 day (see TESTING.md).

    Skips when real AWS creds aren't present so devs without prod access still
    see a meaningful pytest run.
    """
    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError

    aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not aws_key or aws_key == "testing" or not aws_secret:
        pytest.skip("real AWS credentials not set; skipping real_s3 test")

    bucket = os.environ.get("S3_TEST_BUCKET") or settings.S3_BUCKET
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    prefix = f"test/{run_id}/"

    monkeypatch.setattr(settings, "S3_BUCKET", bucket)
    monkeypatch.setenv("S3_BUCKET", bucket)

    # Wrap S3Service.upload_pdf so every key generated during this test is
    # prefixed with `test/<run-id>/` and tracked for cleanup.
    from app.services.storage import S3Service
    original_upload = S3Service.upload_pdf
    uploaded_keys: list[str] = []

    async def tracked_upload(self, pdf_bytes: bytes, report_id: str) -> str:
        prefixed_id = f"{prefix.rstrip('/')}/{report_id}"
        url = await original_upload(self, pdf_bytes, prefixed_id)
        uploaded_keys.append(f"reports/{prefixed_id}.pdf")
        return url

    monkeypatch.setattr(S3Service, "upload_pdf", tracked_upload)

    s3 = boto3.client("s3", region_name=settings.AWS_REGION)

    # We deliberately don't HeadBucket here — that would require s3:ListBucket
    # on the whole bucket. If creds are wrong or the bucket is missing, the
    # first PutObject in the test will fail with a clear error.

    yield {"client": s3, "bucket": bucket, "prefix": prefix, "uploaded_keys": uploaded_keys}

    # ── Teardown: delete every object the test wrote ────────────────────────
    if not uploaded_keys:
        return
    # S3 delete_objects accepts up to 1000 keys per call.
    for batch_start in range(0, len(uploaded_keys), 1000):
        batch = uploaded_keys[batch_start : batch_start + 1000]
        try:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except ClientError as exc:
            # Don't fail the test on cleanup; lifecycle rule will sweep it up within 1 day.
            print(f"[real_s3] cleanup failed for {len(batch)} keys: {exc}")


@pytest.fixture
def ses_capture():
    """moto SES with a helper to read captured messages.

    Use this when code goes through boto3 SES directly (storage uploads,
    legacy paths). Most product emails route through EmailService → use
    `email_capture` instead."""
    from moto import mock_aws
    import boto3

    with mock_aws():
        ses = boto3.client("ses", region_name=settings.AWS_SES_REGION)
        ses.verify_email_identity(EmailAddress=settings.SUPPORT_EMAIL)

        def read_messages():
            backend = boto3.client("ses", region_name=settings.AWS_SES_REGION)
            return backend.list_identities()

        yield ses


@pytest.fixture
def email_capture(monkeypatch):
    """Capture all EmailService.send_html_email calls into a list.

    Returns a list of dicts: {"to": str, "subject": str, "body": str}.
    Resolves the Resend/SES branch at the service boundary so tests don't need
    network or AWS credentials.
    """
    captured: list[dict[str, str]] = []

    async def fake_send(self, to_email: str, subject: str, body_html: str) -> bool:
        captured.append({"to": to_email, "subject": subject, "body": body_html})
        return True

    from app.services.email_service import EmailService
    monkeypatch.setattr(EmailService, "send_html_email", fake_send)

    # Also patch the RFP emailer's inner call, which builds its own HTML
    try:
        from app.services import rfp_express_emailer  # noqa: F401
        # rfp_express_emailer instantiates EmailService internally; the
        # send_html_email patch above covers it.
    except ImportError:
        pass

    return captured


# ── Stripe helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def stripe_test_mode():
    """Skip the test unless a real Stripe test key is configured.

    Lets CI run the full suite while devs without Stripe creds see SKIPPED
    instead of FAILED for the network-touching checkout-session tests."""
    key = os.environ.get("STRIPE_SECRET_KEY") or settings.STRIPE_SECRET_KEY
    if not key or not key.startswith("sk_test_"):
        pytest.skip("STRIPE_SECRET_KEY not a sk_test_ key; skipping Stripe-live test")
    return key


@pytest.fixture
def stripe_session_factory() -> Callable[..., dict[str, Any]]:
    """Build a realistic checkout.session.completed `data.object` dict.

    Tests can pass overrides — metadata is merged, not replaced.
    """
    def _make(
        product_type: str,
        *,
        customer_email: str = "test+e2e@booppa.io",
        report_id: str | None = None,
        vendor_url: str = "https://example.test",
        company_name: str = "Test Co",
        rfp_description: str = "",
        mode: str | None = None,
        session_id: str | None = None,
        amount_total: int = 14900,
        extra_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from app.api.stripe_checkout import MODE_MAP

        metadata = {
            "product_type": product_type,
            "client_ip": "127.0.0.1",
            "customer_email": customer_email,
            "vendor_url": vendor_url,
            "company_name": company_name,
        }
        if report_id:
            metadata["report_id"] = str(report_id)
        if rfp_description:
            metadata["rfp_description"] = rfp_description
        if extra_metadata:
            metadata.update(extra_metadata)

        resolved_mode = mode or MODE_MAP.get(product_type, "payment")
        session = {
            "id": session_id or f"cs_test_{uuid.uuid4().hex[:24]}",
            "object": "checkout.session",
            "mode": resolved_mode,
            "amount_total": amount_total,
            "currency": "sgd",
            "payment_status": "paid",
            "client_reference_id": str(report_id) if report_id else None,
            "customer_email": customer_email,
            "customer_details": {"email": customer_email, "name": company_name},
            "metadata": metadata,
        }
        if resolved_mode == "subscription":
            session["subscription"] = f"sub_test_{uuid.uuid4().hex[:24]}"
            session["customer"] = f"cus_test_{uuid.uuid4().hex[:24]}"
        return session

    return _make


@pytest.fixture
def signed_webhook(monkeypatch) -> Callable[[dict[str, Any]], tuple[bytes, dict[str, str]]]:
    """Sign a Stripe event payload with the configured webhook secret.

    Returns (raw_body_bytes, headers_dict) suitable for direct POST to the
    /webhook route via TestClient. Ensures STRIPE_WEBHOOK_SECRET is set so the
    real signature check in `_stripe_webhook_impl` passes.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET") or "whsec_test_" + "0" * 32
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", secret)

    def _sign(event: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        import hashlib
        import hmac

        # Stripe library exposes `WebhookSignature` in newer versions; emulate
        # the v1 scheme directly so we don't depend on a specific stripe SDK API.
        if "id" not in event:
            event["id"] = f"evt_test_{uuid.uuid4().hex[:24]}"
        if "object" not in event:
            event["object"] = "event"
        body = json.dumps(event, separators=(",", ":")).encode()
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{body.decode()}"
        sig = hmac.new(
            secret.encode(), signed_payload.encode(), hashlib.sha256
        ).hexdigest()
        headers = {"stripe-signature": f"t={timestamp},v1={sig}"}
        return body, headers

    return _sign


@pytest.fixture
def post_webhook(client, signed_webhook):
    """Helper: post a fully-constructed event dict to /webhook.

    `event` should look like a real Stripe event (has `type` and `data.object`).
    Returns the FastAPI response."""
    def _post(event: dict[str, Any]):
        body, headers = signed_webhook(event)
        return client.post(
            "/api/v1/stripe/webhook",
            content=body,
            headers={**headers, "content-type": "application/json"},
        )

    return _post
