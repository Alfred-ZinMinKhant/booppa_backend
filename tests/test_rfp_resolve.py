"""RFP discrepancy-resolution endpoint (Sprint 5d full-stack)."""
import uuid
from datetime import datetime, timezone

from tests._test_helpers import make_user, auth_headers


def _intake(db, user, session_id, status="submitted"):
    from app.core.models_v12 import PendingRfpIntake
    row = PendingRfpIntake(
        user_id=user.id,
        session_id=session_id,
        rfp_product_type="rfp_complete",
        bundle_source="rfp_accelerator",
        vendor_url="https://acme.example",
        company_name="Acme",
        uen="201912345A",
        status=status,
    )
    db.add(row); db.commit(); db.refresh(row)
    return row


def _prior_kit(db, user, session_id):
    from app.core.models import Report
    db.add(Report(
        owner_id=user.id,
        framework="rfp_complete",
        company_name="Acme",
        assessment_data={
            "intake_rfp_description": "Cloud HR platform for MOE",
            "intake_data": {"uen": "201912345A", "iso_status": "pursuing"},
            "discrepancies": ["You declared ISO 27001 but no public evidence was found."],
        },
        status="completed",
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()


def test_resolve_merges_correction_and_requeues(client, test_db, mocker):
    user = make_user(test_db, email="rfp-resolve@booppa.io", plan="rfp_complete", company="Acme")
    sid = f"cs_{uuid.uuid4().hex[:10]}"
    row = _intake(test_db, user, sid)
    _prior_kit(test_db, user, sid)

    fake = mocker.patch("app.workers.tasks.fulfill_rfp_task.delay")
    r = client.post(
        f"/api/rfp-intake/{row.id}/resolve",
        json={"intake_data": {"iso_cert_number": "SG-ISO-9001"}},
        headers=auth_headers(user),
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == sid

    fake.assert_called_once()
    kwargs = fake.call_args.kwargs
    # Prior intake preserved + correction merged + brief recovered.
    assert kwargs["intake_data"]["iso_status"] == "pursuing"
    assert kwargs["intake_data"]["iso_cert_number"] == "SG-ISO-9001"
    assert kwargs["intake_data"]["uen"] == "201912345A"
    assert kwargs["rfp_description"] == "Cloud HR platform for MOE"


def test_resolve_requires_intake_data(client, test_db):
    user = make_user(test_db, email="rfp-resolve2@booppa.io", plan="rfp_complete", company="Acme")
    row = _intake(test_db, user, f"cs_{uuid.uuid4().hex[:10]}")
    r = client.post(
        f"/api/rfp-intake/{row.id}/resolve",
        json={"intake_data": {}},
        headers=auth_headers(user),
    )
    assert r.status_code == 422
