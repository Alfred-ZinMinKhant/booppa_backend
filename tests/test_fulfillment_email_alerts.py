"""Paid-deliverable emails must alert ops when the provider rejects the send.

CLAUDE.md invariant: send_html_email returns False (not raises) on provider
rejection; fulfillment paths must surface that via _alert_payment_fulfillment_issue
so a paying customer is never left with nothing and no human is notified.

The notarization-credits branch in `_fulfill_standalone_no_report` is the cleanly
drivable representative of this pattern (no PDF/S3/scan prerequisites). The
notarization-certificate and PDPA-snapshot branches apply the identical
`if not sent: await _alert_payment_fulfillment_issue(...)` wiring.
"""
import asyncio
import pytest
from sqlalchemy.orm import sessionmaker

import app.services.fulfillment.bundles as wh
from app.core.models import User


def test_credits_email_rejection_alerts_ops(test_db, monkeypatch):
    # Pin the webhook module's session factory to the test engine so the
    # standalone SessionLocal sees the user we create via test_db.
    monkeypatch.setattr(wh, "SessionLocal", sessionmaker(bind=test_db.get_bind()))

    email = "buyer-notarize@test.io"
    test_db.add(User(email=email, hashed_password="x"))
    test_db.commit()

    # Provider rejects the redemption email.
    async def _reject(self, *a, **k):
        return False

    monkeypatch.setattr(wh.EmailService, "send_html_email", _reject)

    # Spy on the ops alert.
    alerts = []

    async def _spy_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(wh, "_alert_payment_fulfillment_issue", _spy_alert)

    handled = asyncio.run(
        wh._fulfill_standalone_no_report(
            product_type="compliance_notarization_1",
            customer_email=email,
            metadata={},
            session_id="cs_test_123",
        )
    )

    assert handled is True
    # Credits were still granted (the email failure must not roll that back).
    test_db.expire_all()
    refreshed = test_db.query(User).filter(User.email == email).first()
    assert refreshed.notarization_credits == 1
    # ...and ops was alerted about the failed delivery.
    assert len(alerts) == 1
    assert alerts[0]["product_type"] == "compliance_notarization_1"
    assert alerts[0]["customer_email"] == email
    assert alerts[0]["session_id"] == "cs_test_123"


def test_credits_email_success_does_not_alert(test_db, monkeypatch):
    monkeypatch.setattr(wh, "SessionLocal", sessionmaker(bind=test_db.get_bind()))

    email = "buyer-notarize-ok@test.io"
    test_db.add(User(email=email, hashed_password="x"))
    test_db.commit()

    async def _accept(self, *a, **k):
        return True

    monkeypatch.setattr(wh.EmailService, "send_html_email", _accept)

    alerts = []

    async def _spy_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(wh, "_alert_payment_fulfillment_issue", _spy_alert)

    asyncio.run(
        wh._fulfill_standalone_no_report(
            product_type="compliance_notarization_1",
            customer_email=email,
            metadata={},
            session_id="cs_test_456",
        )
    )

    # Happy path: no ops alert.
    assert alerts == []
