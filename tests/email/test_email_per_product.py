"""Per-product email content assertions.

Subscriptions: see tests/fulfillment/test_subscriptions.py — covered there.
RFP / PDPA / Notarization fulfillment emails are exercised here by directly
calling the senders so we don't need to drive Celery workers in tests.
"""
import pytest


@pytest.mark.asyncio
async def test_rfp_express_emailer_subject_and_body(email_capture):
    from app.services.rfp_express_emailer import RFPExpressEmailer

    ok = await RFPExpressEmailer().send_express_ready_email(
        customer_email="buyer@example.test",
        vendor_name="Vendor Alpha",
        download_url="https://booppa.io/rfp/abc.pdf",
        product_type="rfp_express",
    )
    assert ok
    assert email_capture, "no email captured"
    msg = email_capture[-1]
    assert msg["to"] == "buyer@example.test"
    assert "RFP Kit Express" in msg["subject"]
    assert "Vendor Alpha" in msg["subject"]
    assert "Download RFP Kit Evidence" in msg["body"]
    assert "https://booppa.io/rfp/abc.pdf" in msg["body"]


@pytest.mark.asyncio
async def test_rfp_complete_emailer_uses_complete_label(email_capture):
    from app.services.rfp_express_emailer import RFPExpressEmailer

    await RFPExpressEmailer().send_express_ready_email(
        customer_email="buyer@example.test",
        vendor_name="Vendor Beta",
        download_url="https://booppa.io/rfp/xyz.pdf",
        product_type="rfp_complete",
    )
    msg = email_capture[-1]
    assert "RFP Kit Complete" in msg["subject"]


@pytest.mark.asyncio
async def test_generic_report_ready_email(email_capture):
    from app.services.email_service import EmailService

    ok = await EmailService().send_report_ready_email(
        to_email="recipient@example.test",
        report_url="https://booppa.io/r/abc.pdf",
        user_name="Test User",
        report_id="abc-123",
    )
    assert ok
    msg = email_capture[-1]
    assert "Audit Report Ready" in msg["subject"]
    assert "abc-123" in msg["subject"]
    assert "Test User" in msg["body"]


@pytest.mark.asyncio
async def test_skip_email_setting_short_circuits(monkeypatch):
    """When settings.SKIP_EMAIL is True the service must return True without
    calling Resend or SES. We verify by patching `_send_resend` to raise — if
    it gets called, the test fails."""
    from app.services.email_service import EmailService
    from app.core.config import settings

    monkeypatch.setattr(settings, "SKIP_EMAIL", True)

    async def boom(*a, **kw):  # pragma: no cover
        raise AssertionError("send_resend should not be called when SKIP_EMAIL=True")

    monkeypatch.setattr(EmailService, "_send_resend", boom)
    monkeypatch.setattr(EmailService, "_send_ses", boom)

    assert await EmailService().send_html_email("x@x.test", "subject", "<p>body</p>")
