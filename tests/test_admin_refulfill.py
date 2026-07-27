"""Admin recovery for paid-but-undelivered orders.

`/admin/failed-reports` only shows `status='failed'`, and `/admin/requeue-report`
409s on `completed`. Neither could see or fix the 2026-07-27 PDPA case: the scan
finished (`completed`), the delivery step never ran, and the buyer got nothing.
These endpoints close that gap — and must refuse to act when the deliverable was
in fact delivered, so a stray click can't duplicate a certificate or re-email.
"""
import pytest

from app.api import admin as admin_mod
from app.main import app
from tests._test_helpers import make_user


@pytest.fixture
def admin_client(client, test_db, mocker):
    """Authenticated admin client with the endpoints' own SessionLocal pinned."""
    app.dependency_overrides[admin_mod._admin_auth] = lambda: True
    mocker.patch.object(admin_mod, "SessionLocal", return_value=test_db)
    mocker.patch.object(test_db, "close", lambda: None)
    yield client
    app.dependency_overrides.pop(admin_mod._admin_auth, None)


def _paid_pdpa_report(db, *, status="completed", scored=True, email="buyer@booppa.io"):
    from app.core.models import Report

    user = make_user(db, email=f"refulfil-{email}", company="Acme")
    ad = {
        "payment_confirmed": True,
        "contact_email": email,
        "stripe_session_id": "cs_test_strand",
    }
    if scored:
        ad["booppa_report"] = {"risk_assessment": {"score": 42}}
    report = Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Acme",
        status=status,
        assessment_data=ad,
    )
    db.add(report)
    db.commit()
    return report


def test_completed_but_undelivered_report_is_listed(admin_client, test_db):
    """The exact shape the old admin views were blind to."""
    report = _paid_pdpa_report(test_db)

    body = admin_client.get("/api/admin/stranded-fulfillments").json()

    ids = {r["report_id"] for r in body["reports"]}
    assert str(report.id) in ids
    row = next(r for r in body["reports"] if r["report_id"] == str(report.id))
    assert row["status"] == "completed"
    assert row["missing"] == "no PDPA CertificateLog"
    assert row["fulfillable"] is True
    assert row["stripe_session_id"] == "cs_test_strand"


def test_delivered_report_is_not_listed(admin_client, test_db):
    from app.core.models import CertificateLog

    report = _paid_pdpa_report(test_db, email="delivered@booppa.io")
    test_db.add(CertificateLog(
        vendor_id=report.owner_id, certificate_type="PDPA", report_id=report.id,
    ))
    test_db.commit()

    body = admin_client.get("/api/admin/stranded-fulfillments").json()

    assert str(report.id) not in {r["report_id"] for r in body["reports"]}


def test_unscored_report_is_listed_but_flagged_unfulfillable(admin_client, test_db):
    """Re-queuing delivery can't help — the scan itself never produced a score."""
    report = _paid_pdpa_report(test_db, scored=False, email="unscored@booppa.io")

    body = admin_client.get("/api/admin/stranded-fulfillments").json()

    row = next(r for r in body["reports"] if r["report_id"] == str(report.id))
    assert row["fulfillable"] is False


def test_refulfill_queues_the_delivery_task_not_the_scan(admin_client, test_db, mocker):
    from app.workers import tasks as tasks_mod

    report = _paid_pdpa_report(test_db, email="requeue@booppa.io")
    deliver = mocker.patch.object(tasks_mod.fulfill_pdpa_task, "delay")
    rescan = mocker.patch.object(tasks_mod.process_report_task, "apply_async")

    resp = admin_client.post("/api/admin/refulfill", json={"report_id": str(report.id)})

    assert resp.status_code == 200
    assert resp.json()["queued"] == "fulfill_pdpa_task"
    deliver.assert_called_once_with(str(report.id), "requeue@booppa.io")
    rescan.assert_not_called()


def test_refulfill_refuses_an_already_delivered_report(admin_client, test_db, mocker):
    from app.core.models import CertificateLog
    from app.workers import tasks as tasks_mod

    report = _paid_pdpa_report(test_db, email="dupe@booppa.io")
    test_db.add(CertificateLog(
        vendor_id=report.owner_id, certificate_type="PDPA", report_id=report.id,
    ))
    test_db.commit()
    deliver = mocker.patch.object(tasks_mod.fulfill_pdpa_task, "delay")

    resp = admin_client.post("/api/admin/refulfill", json={"report_id": str(report.id)})

    assert resp.status_code == 409
    assert "Already delivered" in resp.json()["detail"]
    deliver.assert_not_called()


def test_refulfill_404s_on_unknown_report(admin_client, test_db):
    import uuid

    resp = admin_client.post(
        "/api/admin/refulfill", json={"report_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


def test_v1_mount_exists_for_the_admin_panel(admin_client, test_db):
    """The Next.js admin proxy builds `${apiUrl}/api/v1/admin/...`.

    Pinned because the panel calls the /api/v1 mount exclusively — losing it
    would break the page while every /api/admin test here still passed.
    """
    resp = admin_client.get("/api/v1/admin/stranded-fulfillments")
    assert resp.status_code == 200
    assert "reports" in resp.json()


def test_endpoints_require_admin_auth(client):
    """No dependency override here — the real _admin_auth must reject."""
    assert client.get("/api/admin/stranded-fulfillments").status_code in (401, 403)
    assert client.post(
        "/api/admin/refulfill", json={"report_id": "x"}
    ).status_code in (401, 403)
