"""Vendor Proof procurement-readiness honesty (forensic-audit integrity finding).

A flat "Conditional" readiness + green "Verified" badge awarded to EVERY buyer —
even one whose PDPA scan shows critical gaps — misleads procurement officers into
reading the badge as a compliance endorsement. Readiness + confidence must
reflect the vendor's ACTUAL latest PDPA compliance.
"""
import asyncio


def _make_user(db, email):
    from app.core.models import User
    u = User(
        email=email, hashed_password="x", role="VENDOR",
        company="Crayon Singapore", website="https://crayon.com/",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _vendor_proof_report(db, owner_id):
    from app.core.models import Report
    r = Report(
        owner_id=owner_id, framework="vendor_proof",
        company_name="Crayon Singapore", status="pending",
        assessment_data={"contact_email": None},
    )
    db.add(r); db.commit(); db.refresh(r)
    return r


def _run(report_id, email, mocker):
    async def fake_email(self, *a, **k):
        return True
    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)
    mocker.patch("app.services.scoring.VendorScoreEngine.update_vendor_score", return_value=None)
    from app.api.stripe_webhook import _fulfill_vendor_proof
    asyncio.run(_fulfill_vendor_proof(str(report_id), email))


def _snapshot(db, vendor_id):
    from app.core.models import VendorStatusSnapshot
    db.expire_all()
    return (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == vendor_id)
        .first()
    )


def test_readiness_not_ready_for_critical_compliance(test_db, mocker):
    """A vendor whose latest PDPA scan is 8/100 must NOT be marked 'Conditional'
    — it should be NOT_READY with the real (low) confidence, so the badge can't
    pose as a compliance pass."""
    from app.core.models import Report

    email = "vp+critical@booppa.io"
    user = _make_user(test_db, email)
    test_db.add(Report(
        owner_id=user.id, framework="pdpa_quick_scan",
        company_name="Crayon Singapore", status="completed",
        assessment_data={"compliance_score": 8},
    ))
    test_db.commit()
    rpt = _vendor_proof_report(test_db, user.id)

    _run(rpt.id, email, mocker)

    snap = _snapshot(test_db, user.id)
    assert snap is not None
    assert snap.procurement_readiness == "NOT_READY"
    assert int(snap.confidence_score) == 8


def test_readiness_ready_for_strong_compliance(test_db, mocker):
    from app.core.models import Report

    email = "vp+strong@booppa.io"
    user = _make_user(test_db, email)
    test_db.add(Report(
        owner_id=user.id, framework="pdpa_quick_scan",
        company_name="Crayon Singapore", status="completed",
        assessment_data={"compliance_score": 82},
    ))
    test_db.commit()
    rpt = _vendor_proof_report(test_db, user.id)

    _run(rpt.id, email, mocker)

    snap = _snapshot(test_db, user.id)
    assert snap is not None
    assert snap.procurement_readiness == "READY"
    assert int(snap.confidence_score) == 82


def test_readiness_conditional_when_no_pdpa_scan(test_db, mocker):
    """No PDPA scan → identity verified only, compliance not yet assessed."""
    email = "vp+noscan@booppa.io"
    user = _make_user(test_db, email)
    rpt = _vendor_proof_report(test_db, user.id)

    _run(rpt.id, email, mocker)

    snap = _snapshot(test_db, user.id)
    assert snap is not None
    assert snap.procurement_readiness == "CONDITIONAL"
    assert int(snap.confidence_score) == 30
