"""On-chain anchoring failure (gas wallet empty) must notify the buyer about the
delay exactly once, not on every Celery retry.

process_report_workflow wraps the anchor call and routes the failure through the
deduped alert_payment_fulfillment_issue helper (see app/workers/tasks.py). The
guarantee hinges on passing a STABLE reason string: the raw insufficient-funds
message embeds changing balance/cost numbers, and reason is part of the alert's
dedup key, so the raw string would defeat the once-only guarantee.

These pin the dedup contract at the helper level (driving the whole report
workflow needs scan/PDF/S3 prerequisites this doesn't).
"""
import asyncio
import uuid

import app.services.fulfillment.helpers as helpers

_DELAY_SUBJECT = "one small delay"


def _spy_sends(monkeypatch):
    sends: list[str] = []

    async def _spy(self, to_email=None, subject=None, body_html=None, **k):
        sends.append(subject or "")
        return True

    monkeypatch.setattr(helpers.EmailService, "send_html_email", _spy)
    return sends


def test_stable_reason_sends_buyer_delay_email_once(monkeypatch):
    sends = _spy_sends(monkeypatch)

    # Unique identity per run so leftover dedup keys (Redis or file fallback)
    # from a previous run can't mask the assertion.
    email = f"buyer-{uuid.uuid4().hex}@test.io"
    sid = f"report-{uuid.uuid4().hex}"
    reason = "blockchain anchoring failed — gas wallet out of funds"

    # Two "retries" with the SAME stable reason but different volatile error data
    # (the balance number that really does change between attempts).
    for balance in (108912438303543402, 95000000000000000):
        asyncio.run(
            helpers._alert_payment_fulfillment_issue(
                reason=reason,
                product_type="pdpa_quick_scan",
                customer_email=email,
                session_id=sid,
                extra={"balance": balance},
            )
        )

    delay_sends = [s for s in sends if _DELAY_SUBJECT in s.lower()]
    assert len(delay_sends) == 1, f"expected exactly one buyer delay email, got {sends}"


def test_varying_reason_defeats_dedup(monkeypatch):
    """Documents WHY tasks.py must pass a fixed reason: folding the changing
    balance/cost numbers into `reason` makes every retry a new dedup identity and
    re-sends the buyer a delay email each time."""
    sends = _spy_sends(monkeypatch)

    email = f"buyer-{uuid.uuid4().hex}@test.io"
    sid = f"report-{uuid.uuid4().hex}"

    for balance in (108912438303543402, 95000000000000000):
        asyncio.run(
            helpers._alert_payment_fulfillment_issue(
                reason=f"insufficient funds ... balance {balance}",  # anti-pattern
                product_type="pdpa_quick_scan",
                customer_email=email,
                session_id=sid,
            )
        )

    delay_sends = [s for s in sends if _DELAY_SUBJECT in s.lower()]
    assert len(delay_sends) == 2, f"varying reason should re-send, got {sends}"
