"""PDPA Quick Scan fulfillment gate.

Regression cover for the stranded-payment bug: `_fulfill_pdpa` decided the scan
was complete by reading top-level `risk_score` / `risk_assessment.score`, but
`process_report_task` persists the AI score nested under
`booppa_report.risk_assessment.score`. The gate was therefore unsatisfiable for
every fresh stub report — the buyer paid, ten retries ran a full AI scan each,
and Celery gave up silently.
"""
import asyncio

import pytest

from app.services.pdpa_findings import resolve_pdpa_risk_score, resolve_pdpa_score
from tests._test_helpers import make_user


# ── Resolvers ────────────────────────────────────────────────────────────────

def test_risk_score_reads_the_nested_shape_the_scan_actually_writes():
    """The exact assessment_data shape process_report_task persists."""
    ad = {"booppa_report": {"risk_assessment": {"score": 42, "level": "MEDIUM"}}}
    assert resolve_pdpa_risk_score(ad) == 42
    # …and the compliance view inverts it.
    assert resolve_pdpa_score(ad) == 58


@pytest.mark.parametrize(
    "ad,expected",
    [
        ({"overall_risk_score": 30}, 30),
        ({"score": 25}, 25),
        ({"risk_score": 10}, 10),
        ({"risk_assessment": {"score": 60}}, 60),
        # Precedence: top-level overall_risk_score wins over the nested score.
        ({"overall_risk_score": 30, "booppa_report": {"risk_assessment": {"score": 90}}}, 30),
        # Clamping and junk.
        ({"overall_risk_score": 150}, 100),
        ({"overall_risk_score": "not-a-number"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_risk_score_key_ladder(ad, expected):
    assert resolve_pdpa_risk_score(ad) == expected


def test_compliance_score_still_prefers_persisted_compliance_score():
    """Unchanged behaviour: an explicit compliance_score is displayed verbatim."""
    assert resolve_pdpa_score({"compliance_score": 66, "overall_risk_score": 90}) == 66
    assert resolve_pdpa_score({"overall_risk_score": 34}) == 66


# ── Fulfillment gate ─────────────────────────────────────────────────────────

def _make_report(db, *, status, assessment):
    from app.core.models import Report

    user = make_user(db, email=f"pdpa-gate-{status}@booppa.io", company="Acme")
    report = Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Acme",
        company_website="https://example.test",
        status=status,
        assessment_data=assessment,
    )
    db.add(report)
    db.commit()
    return report


@pytest.fixture
def pinned_session(test_db, mocker):
    """Point _fulfill_pdpa's own SessionLocal at the test session."""
    import app.services.fulfillment.single_products as sp

    mocker.patch.object(sp, "SessionLocal", return_value=test_db)
    mocker.patch.object(test_db, "close", lambda: None)
    return test_db


def test_nested_score_proceeds_to_pdf_instead_of_raising(pinned_session, mocker):
    """The core regression: a nested-only score must satisfy the gate."""
    import app.services.fulfillment.single_products as sp
    from app.workers import tasks as tasks_mod

    report = _make_report(
        pinned_session,
        status="completed",
        assessment={
            "payment_confirmed": True,
            "contact_email": "buyer@booppa.io",
            "booppa_report": {"risk_assessment": {"score": 42, "level": "MEDIUM"}},
        },
    )

    gen_pdf = mocker.patch.object(sp.PDFService, "generate_pdf", return_value=b"%PDF-fake")
    mocker.patch.object(sp.S3Service, "upload_pdf", new=mocker.AsyncMock(return_value="https://s3/x.pdf"))
    mocker.patch.object(sp.EmailService, "send_html_email", new=mocker.AsyncMock(return_value=True))
    mocker.patch.object(sp, "_maybe_fire_cover_sheet", lambda *a, **k: None)
    chain = mocker.patch.object(tasks_mod.process_report_task, "apply_async")

    asyncio.run(sp._fulfill_pdpa(str(report.id), "buyer@booppa.io", raise_if_incomplete=True))

    gen_pdf.assert_called_once()
    # It must NOT have fallen into the "scan not complete" branch.
    chain.assert_not_called()
    assert gen_pdf.call_args[0][0]["risk_score"] == 42


def test_terminal_status_alerts_and_does_not_retry(pinned_session, mocker):
    """A 403 site can never produce a score — alert, don't loop."""
    import app.services.fulfillment.single_products as sp
    from app.workers import tasks as tasks_mod

    report = _make_report(
        pinned_session,
        status="site_inaccessible",
        assessment={
            "payment_confirmed": True,
            "contact_email": "buyer@booppa.io",
            "stripe_session_id": "cs_test_123",
        },
    )

    alert = mocker.patch.object(sp, "_alert_payment_fulfillment_issue", new=mocker.AsyncMock())
    chain = mocker.patch.object(tasks_mod.process_report_task, "apply_async")

    # raise_if_incomplete=True and yet it must return cleanly.
    asyncio.run(sp._fulfill_pdpa(str(report.id), "buyer@booppa.io", raise_if_incomplete=True))

    chain.assert_not_called()
    alert.assert_awaited_once()
    kwargs = alert.await_args.kwargs
    assert "site_inaccessible" in kwargs["reason"]
    # The alert is now attributable to a Stripe session (was "(missing)").
    assert kwargs["session_id"] == "cs_test_123"


def _terminal_report(db):
    return _make_report(
        db,
        status="site_inaccessible",
        assessment={
            "payment_confirmed": True,
            "contact_email": "buyer@booppa.io",
            "stripe_session_id": "cs_test_unscannable",
            "site_inaccessible": True,
            "site_inaccessible_reason": "Website returned HTTP 403 Forbidden.",
        },
    )


@pytest.fixture
def unscannable(pinned_session, mocker):
    """Wire up the terminal-state path with everything external stubbed."""
    import app.services.fulfillment.single_products as sp
    import app.services.refund_service as refunds

    report = _terminal_report(pinned_session)
    refund = mocker.patch.object(
        refunds,
        "refund_for_session",
        return_value={
            "refunded": True,
            "refund_id": "re_1",
            "amount": 4900,
            "currency": "sgd",
            "skipped_reason": None,
        },
    )
    send = mocker.patch.object(
        sp.EmailService, "send_html_email", new=mocker.AsyncMock(return_value=True)
    )
    alert = mocker.patch.object(
        sp, "_alert_payment_fulfillment_issue", new=mocker.AsyncMock()
    )
    return mocker.Mock(report=report, refund=refund, send=send, alert=alert, sp=sp)


def test_unscannable_site_is_refunded_and_the_buyer_is_told(unscannable, pinned_session):
    """The incident fix: a paid scan we cannot perform must not silently keep the money."""
    asyncio.run(
        unscannable.sp._fulfill_pdpa(
            str(unscannable.report.id), "buyer@booppa.io", raise_if_incomplete=True
        )
    )

    unscannable.refund.assert_called_once()
    assert unscannable.refund.call_args[0][0] == "cs_test_unscannable"

    unscannable.send.assert_awaited_once()
    body = unscannable.send.await_args.kwargs["body_html"]
    assert "refunded your payment in full" in body
    # The buyer gets the report the scan already produced.
    assert f"/reports/{unscannable.report.id}/download" in body


def test_the_misleading_delay_email_is_suppressed(unscannable):
    """'We'll follow up within a few hours' is a promise nothing can keep here.

    Ops must still be paged, but with notify_customer=False.
    """
    asyncio.run(
        unscannable.sp._fulfill_pdpa(str(unscannable.report.id), "buyer@booppa.io")
    )

    unscannable.alert.assert_awaited_once()
    kwargs = unscannable.alert.await_args.kwargs
    assert kwargs["notify_customer"] is False
    assert kwargs["session_id"] == "cs_test_unscannable"
    assert kwargs["extra"]["refunded"] is True


def test_no_certificate_is_issued_for_an_unscannable_site(unscannable, pinned_session):
    """There is no score to certify — and a cert would hide it from the admin view."""
    from app.core.models import CertificateLog

    asyncio.run(
        unscannable.sp._fulfill_pdpa(str(unscannable.report.id), "buyer@booppa.io")
    )

    pinned_session.expire_all()
    certs = (
        pinned_session.query(CertificateLog)
        .filter(CertificateLog.report_id == unscannable.report.id)
        .all()
    )
    assert certs == []


def test_terminal_path_never_refunds_or_emails_twice(unscannable):
    """fulfill_pdpa_task retries; a second refund would be real money."""
    for _ in range(3):
        asyncio.run(
            unscannable.sp._fulfill_pdpa(str(unscannable.report.id), "buyer@booppa.io")
        )

    assert unscannable.refund.call_count == 1
    assert unscannable.send.await_count == 1


def test_a_rejected_email_leaves_the_notice_unclaimed(pinned_session, mocker):
    """send_html_email returns False on rejection without raising.

    If we recorded the notice anyway the buyer would never be told at all.
    """
    import app.services.fulfillment.single_products as sp
    import app.services.refund_service as refunds

    report = _terminal_report(pinned_session)
    mocker.patch.object(
        refunds,
        "refund_for_session",
        return_value={"refunded": True, "refund_id": "re_1", "skipped_reason": None},
    )
    mocker.patch.object(sp, "_alert_payment_fulfillment_issue", new=mocker.AsyncMock())
    send = mocker.patch.object(
        sp.EmailService, "send_html_email", new=mocker.AsyncMock(return_value=False)
    )

    asyncio.run(sp._fulfill_pdpa(str(report.id), "buyer@booppa.io"))

    pinned_session.expire_all()
    assert not report.assessment_data.get("inaccessible_notice_sent_at")
    # A retry can therefore still reach the buyer.
    asyncio.run(sp._fulfill_pdpa(str(report.id), "buyer@booppa.io"))
    assert send.await_count == 2


def test_second_pass_is_a_no_op(pinned_session, mocker):
    """Re-entry must not duplicate the CertificateLog or re-send the email."""
    import app.services.fulfillment.single_products as sp
    from app.core.models import CertificateLog

    report = _make_report(
        pinned_session,
        status="completed",
        assessment={
            "payment_confirmed": True,
            "contact_email": "buyer@booppa.io",
            "booppa_report": {"risk_assessment": {"score": 42}},
        },
    )

    mocker.patch.object(sp.PDFService, "generate_pdf", return_value=b"%PDF-fake")
    mocker.patch.object(sp.S3Service, "upload_pdf", new=mocker.AsyncMock(return_value="https://s3/x.pdf"))
    send = mocker.patch.object(sp.EmailService, "send_html_email", new=mocker.AsyncMock(return_value=True))
    mocker.patch.object(sp, "_maybe_fire_cover_sheet", lambda *a, **k: None)

    for _ in range(2):
        asyncio.run(sp._fulfill_pdpa(str(report.id), "buyer@booppa.io"))

    pinned_session.expire_all()
    certs = (
        pinned_session.query(CertificateLog)
        .filter(CertificateLog.report_id == report.id)
        .all()
    )
    assert len(certs) == 1
    assert send.await_count == 1
