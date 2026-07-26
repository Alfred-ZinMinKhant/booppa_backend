"""Fulfillment-alert hygiene.

Two production regressions are pinned here, both seen in the vendor_proof
retry storm:

1. The dedupe key was built from the raw `reason`, which for a SQLAlchemy error
   embeds a `[parameters: {...}]` tail that changes on every Celery retry —
   so the "we received your payment, one small delay" email went to the buyer
   once per attempt instead of once per failure.
2. The ops alert subject was the raw exception text, complete with newlines and
   kilobytes of SQL. SES rejected the send outright
   (`InvalidParameterValue: Invalid message subject`), so the only signal that
   fulfillment was broken never left the box.
"""
import pytest

from app.services.fulfillment import helpers

_SQL_ERROR = (
    "Vendor Proof fulfillment raised before delivery: "
    "(psycopg2.errors.StringDataRightTruncation) value too long for type "
    "character varying(66)\n\n"
    "[SQL: UPDATE reports SET assessment_data=%(assessment_data)s::JSON, "
    "tx_hash=%(tx_hash)s WHERE reports.id = %(reports_id)s::UUID]\n"
)


def _reason_with_params(tx: str, ts: str) -> str:
    """Same failure, different retry — only the parameters tail differs."""
    return _SQL_ERROR + (
        f"[parameters: {{'tx_hash': '{tx}', 'updated_at': '{ts}'}}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/9h9h)"
    )


@pytest.fixture
def sends(monkeypatch):
    """Capture every send; give the alert helper a clean in-memory cache."""
    store: dict[str, dict] = {}
    captured: list[dict] = []

    def _add(key, value, ttl=None):
        if key in store:
            return False
        store[key] = value
        return True

    monkeypatch.setattr("app.core.cache.cache.add", _add)
    monkeypatch.setattr("app.core.cache.cache.delete", lambda key: bool(store.pop(key, None)))

    async def _send(self, to_email, subject, body_html, **kwargs):
        captured.append({"to": to_email, "subject": subject, "body": body_html})
        return True

    monkeypatch.setattr(helpers.EmailService, "send_html_email", _send, raising=False)
    return captured


@pytest.mark.asyncio
async def test_retries_of_the_same_failure_email_the_buyer_once(sends):
    for i in range(4):
        await helpers._alert_payment_fulfillment_issue(
            reason=_reason_with_params(f"demo-0x{i:064x}", f"2026-07-26 10:5{i}:17"),
            product_type="vendor_proof",
            customer_email="buyer@example.com",
            session_id=None,
        )

    customer = [s for s in sends if s["to"] == "buyer@example.com"]
    assert len(customer) == 1, (
        f"buyer received {len(customer)} 'small delay' emails across 4 retries — "
        "the dedupe key is varying with the SQL parameters again"
    )


@pytest.mark.asyncio
async def test_ops_subject_is_single_line_and_bounded(sends):
    await helpers._alert_payment_fulfillment_issue(
        reason=_reason_with_params("demo-0xabc", "2026-07-26 10:49:08"),
        product_type="vendor_proof",
        customer_email="buyer@example.com",
    )

    ops = [s for s in sends if s["to"] != "buyer@example.com"]
    assert ops, "no ops alert was sent"
    subject = ops[0]["subject"]
    assert "\n" not in subject and "\r" not in subject
    assert "[SQL:" in subject or "character varying" in subject, (
        "the subject should still identify the failure"
    )


@pytest.mark.asyncio
async def test_adapter_sanitizes_any_subject(monkeypatch):
    """Backstop: the email adapter itself must never hand SES a bad subject."""
    from app.adapters.resend_email import ResendEmailAdapter

    seen: dict = {}

    async def _fake_resend(self, to_email, subject, body_html, attachments, headers):
        seen["subject"] = subject
        return True

    monkeypatch.setattr(ResendEmailAdapter, "_send_resend", _fake_resend)
    monkeypatch.setattr(ResendEmailAdapter, "_send_ses", _fake_resend)
    monkeypatch.setattr("app.core.config.settings.SKIP_EMAIL", False, raising=False)

    await ResendEmailAdapter().send_html_email(
        to_email="ops@example.com",
        subject="[FULFILLMENT] boom\nline two\r\n" + ("x" * 500),
        body_html="<p>hi</p>",
    )

    subject = seen["subject"]
    assert "\n" not in subject and "\r" not in subject
    assert len(subject) <= 200, f"subject is {len(subject)} chars — SES will reject it"


@pytest.mark.asyncio
async def test_distinct_failures_still_alert_separately(sends):
    for reason in ("anchoring ran out of gas", "S3 upload failed"):
        await helpers._alert_payment_fulfillment_issue(
            reason=reason,
            product_type="vendor_proof",
            customer_email="buyer@example.com",
        )

    customer = [s for s in sends if s["to"] == "buyer@example.com"]
    assert len(customer) == 2
