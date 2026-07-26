"""A rejected RFP kit delivery email must not report success.

`EmailService.send_html_email` returns False on provider rejection instead of
raising, and the emitter discarded that value — logging "delivery email sent"
and returning True, so a bounced paid kit looked fulfilled to every caller.
"""

import pytest

from app.services import rfp_express_emailer as mod
from app.services.rfp_express_emailer import RFPExpressEmailer


@pytest.fixture
def alert_capture(monkeypatch):
    calls = []

    async def _fake_alert(**kwargs):
        calls.append(kwargs)

    import app.services.fulfillment.helpers as helpers

    monkeypatch.setattr(helpers, "_alert_payment_fulfillment_issue", _fake_alert)
    return calls


def _patch_send(monkeypatch, result: bool):
    sent = []

    class _Svc:
        async def send_html_email(self, **kwargs):
            sent.append(kwargs)
            return result

    monkeypatch.setattr(mod, "EmailService", lambda: _Svc(), raising=False)

    import app.services.email_service as email_service

    monkeypatch.setattr(email_service, "EmailService", lambda: _Svc())
    return sent


@pytest.mark.asyncio
async def test_returns_false_and_alerts_when_provider_rejects(monkeypatch, alert_capture):
    _patch_send(monkeypatch, False)

    ok = await RFPExpressEmailer().send_express_ready_email(
        customer_email="buyer@example.com",
        vendor_name="Booppa QA",
        download_url="https://example.com/kit.pdf",
    )

    assert ok is False
    assert len(alert_capture) == 1
    assert alert_capture[0]["customer_email"] == "buyer@example.com"


@pytest.mark.asyncio
async def test_returns_true_on_successful_send(monkeypatch, alert_capture):
    sent = _patch_send(monkeypatch, True)

    ok = await RFPExpressEmailer().send_express_ready_email(
        customer_email="buyer@example.com",
        vendor_name="Booppa QA",
        download_url="https://example.com/kit.pdf",
    )

    assert ok is True
    assert len(sent) == 1
    assert not alert_capture
