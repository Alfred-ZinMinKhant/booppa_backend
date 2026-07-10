"""PDPA Level-2 self-declaration: generator validation/PDF, intake API, and the
fulfillment task.

Covers app/services/pdpa_declaration_generator.py, app/api/pdpa_declaration_intake.py,
and app/workers/tasks.py:fulfill_pdpa_declaration_task.
"""
import asyncio

from tests._test_helpers import make_user, auth_headers


_VALID_ROW = {
    "processing_purpose": "Payroll",
    "lawful_basis": "Legal or Regulatory Obligation",
    "data_categories": "Name, NRIC, bank account",
    "data_subjects": "Employees",
    "recipients": "CPF Board, IRAS",
    "retention_period": "7 years after termination",
    "safeguards": "Access control & <encryption>",
}


# ── Generator ────────────────────────────────────────────────────────────────

def test_validate_rejects_empty_and_missing_fields():
    from app.services.pdpa_declaration_generator import validate_pdpa_declaration
    assert validate_pdpa_declaration([]) == ["Add at least one processing activity."]
    errs = validate_pdpa_declaration([{**_VALID_ROW, "processing_purpose": ""}])
    assert any("Processing purpose" in e for e in errs)


def test_validate_rejects_unknown_legal_basis():
    from app.services.pdpa_declaration_generator import validate_pdpa_declaration
    errs = validate_pdpa_declaration([{**_VALID_ROW, "lawful_basis": "Vibes"}])
    assert any("not a recognised PDPA legal basis" in e for e in errs)


def test_validate_accepts_valid_row_and_pdf_renders():
    from app.services.pdpa_declaration_generator import (
        validate_pdpa_declaration, generate_pdpa_declaration_pdf,
    )
    assert validate_pdpa_declaration([_VALID_ROW]) == []
    pdf = generate_pdpa_declaration_pdf("Acme & Co <Ltd>", "201912345A", [_VALID_ROW])
    assert pdf[:4] == b"%PDF"


# ── Intake API ───────────────────────────────────────────────────────────────

def test_schema_is_public(client):
    r = client.get("/api/pdpa-declaration/schema")
    assert r.status_code == 200
    body = r.json()
    assert [f["key"] for f in body["fields"]][0] == "processing_purpose"
    assert "Consent" in body["legal_basis_options"]


def test_intake_requires_auth(client):
    assert client.get("/api/pdpa-declaration/intake").status_code == 401


def test_save_draft_then_submit(client, test_db, mocker):
    # submit queues a Celery task — don't hit a real broker.
    from app.workers import tasks as tasks_mod
    mocker.patch.object(tasks_mod.fulfill_pdpa_declaration_task, "apply_async", return_value=None)

    user = make_user(test_db, email="pdpa-l2@booppa.io")
    headers = auth_headers(user)

    save = client.post("/api/pdpa-declaration/intake",
                       json={"activities": [_VALID_ROW]}, headers=headers)
    assert save.status_code == 200 and save.json()["saved"] == 1

    got = client.get("/api/pdpa-declaration/intake", headers=headers).json()
    assert len(got["activities"]) == 1 and got["submitted"] is False

    sub = client.post("/api/pdpa-declaration/intake/submit", headers=headers)
    assert sub.status_code == 200 and sub.json()["status"] == "submitted"
    assert tasks_mod.fulfill_pdpa_declaration_task.apply_async.called

    # Submitted rows are locked from re-edit.
    again = client.post("/api/pdpa-declaration/intake",
                        json={"activities": [_VALID_ROW]}, headers=headers)
    assert again.status_code == 409


def test_submit_without_draft_is_422(client, test_db):
    user = make_user(test_db, email="pdpa-l2-empty@booppa.io")
    r = client.post("/api/pdpa-declaration/intake/submit", headers=auth_headers(user))
    assert r.status_code == 422


def test_status_reflects_lifecycle(client, test_db):
    """status: empty → draft → completed (once the anchored Report exists)."""
    from app.core.models import Report
    from app.core.models import PdpaSelfDeclaration

    user = make_user(test_db, email="pdpa-l2-status@booppa.io", company="Acme")
    headers = auth_headers(user)

    empty = client.get("/api/pdpa-declaration/status", headers=headers).json()
    assert empty["completed"] is False and empty["submitted"] is False

    # Submitted declaration but no Report yet → submitted, not completed.
    test_db.add(PdpaSelfDeclaration(user_id=user.id, source="pdpa_quick_scan",
                                    status="submitted", **_VALID_ROW))
    test_db.commit()
    mid = client.get("/api/pdpa-declaration/status", headers=headers).json()
    assert mid["submitted"] is True and mid["completed"] is False

    # Anchored Report present → completed with tx_hash surfaced.
    test_db.add(Report(owner_id=user.id, framework="pdpa_self_declaration",
                       company_name="Acme", status="completed", tx_hash="0xpdpa",
                       audit_hash="a" * 64,
                       assessment_data={"s3_key": "reports/x.pdf",
                                        "blockchain_anchored_at": "2026-06-20T00:00:00+00:00"}))
    test_db.commit()
    done = client.get("/api/pdpa-declaration/status", headers=headers).json()
    assert done["completed"] is True and done["tx_hash"] == "0xpdpa"


# ── Fulfillment task ─────────────────────────────────────────────────────────

def test_fulfillment_creates_anchored_report(test_db, mocker):
    from app.core.models import Report
    from app.core.models import PdpaSelfDeclaration

    user = make_user(test_db, email="pdpa-l2-fulfil@booppa.io", company="Acme")
    test_db.add(PdpaSelfDeclaration(user_id=user.id, source="pdpa_quick_scan",
                                    status="submitted", **_VALID_ROW))
    test_db.commit()

    async def fake_upload(self, pdf_bytes, report_id):
        return f"https://s3.example/{report_id}.pdf"

    async def fake_anchor(self, evidence_hash, metadata="", force=False):
        return "0xpdpa"

    async def fake_attach(self, *a, **k):
        return True

    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.blockchain.BlockchainService.anchor_evidence", fake_anchor)
    mocker.patch("app.services.email_service.EmailService.send_with_pdf_attachment", fake_attach)
    # The task opens its own SessionLocal — point it at the test session's bind.
    mocker.patch("app.workers.tasks.SessionLocal", return_value=test_db)
    # Don't let the task close the shared test session.
    mocker.patch.object(test_db, "close", lambda: None)

    from app.workers.tasks import fulfill_pdpa_declaration_task
    fulfill_pdpa_declaration_task(str(user.id), user.email)

    test_db.expire_all()
    rpt = (
        test_db.query(Report)
        .filter(Report.owner_id == user.id, Report.framework == "pdpa_self_declaration")
        .first()
    )
    assert rpt is not None
    assert rpt.tx_hash == "0xpdpa"
    assert rpt.audit_hash and len(rpt.audit_hash) == 64
    assert rpt.status == "completed"
