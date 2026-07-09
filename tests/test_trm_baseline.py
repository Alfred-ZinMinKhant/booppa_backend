"""MAS TRM Baseline Assessment deliverable (Phase E of the audit remediation).

Suites initialise 13 TRM domains; previously the buyer got only an email saying
so. This verifies the tangible PDF artifact renders all domains and that the
delivery task generates + uploads + emails it.
"""
from io import BytesIO

from pypdf import PdfReader

from app.core.models import MAS_TRM_DOMAINS


def test_baseline_pdf_renders_all_domains():
    from app.services.trm_baseline_generator import generate_trm_baseline_pdf

    controls = [
        {"domain": d, "control_ref": f"TRM-{i}", "status": "not_started"}
        for i, d in enumerate(MAS_TRM_DOMAINS, 1)
    ]
    pdf = generate_trm_baseline_pdf({
        "company_name": "Funding Societies",
        "plan_label": "Pro Suite",
        "controls": controls,
        "provisioning": [
            {"capability": "SSO — SAML 2.0 / OIDC", "status": "Ready", "detail": "Configure at booppa.io/vendor/sso"},
            {"capability": "White-label reports", "status": "Ready", "detail": "Enable at booppa.io/vendor/profile"},
        ],
    })
    assert pdf.startswith(b"%PDF")

    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "MAS TRM Baseline" in text
    assert "Funding Societies" in text
    # A representative sample of the 13 domains must be present.
    for domain in ("Cyber Security", "Incident Management", "Cloud Computing"):
        assert domain in text, f"missing domain: {domain}"
    assert "Not Started" in text
    # New: initial gap analysis + provisioning evidence sections.
    assert "Initial Gap Analysis" in text
    assert "Configuration" in text and "Provisioning" in text
    assert "SSO" in text and "White-label" in text


def test_baseline_task_generates_uploads_and_emails(test_db, mocker):
    from app.core.models import User
    from app.core.models import Organisation, TrmControl

    captured = {}

    async def fake_upload(self, pdf_bytes, report_id):
        captured["pdf"] = pdf_bytes
        captured["report_id"] = report_id
        return f"https://s3.example/{report_id}.pdf"

    async def fake_email(self, to_email, subject, body_html):
        captured["to"] = to_email
        captured["subject"] = subject
        captured["body"] = body_html
        return True

    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)

    user = User(
        email="suite+trm@booppa.io",
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="pro_suite",
        company="Funding Societies",
        website="https://example.test",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    org = Organisation(name="Funding Societies", slug=f"fs-{user.id}", owner_user_id=user.id)
    test_db.add(org)
    test_db.commit()
    test_db.refresh(org)
    for i, d in enumerate(MAS_TRM_DOMAINS, 1):
        test_db.add(TrmControl(organisation_id=org.id, domain=d, control_ref=f"TRM-{i}", status="not_started"))
    test_db.commit()

    from app.workers.tasks import run_suite_trm_baseline_for_user
    run_suite_trm_baseline_for_user(str(user.id))

    assert captured.get("pdf", b"").startswith(b"%PDF")
    assert captured["report_id"] == f"trm-baseline-{user.id}"
    assert captured["to"] == "suite+trm@booppa.io"
    assert "TRM Baseline" in captured["subject"]
    assert "Download your TRM Baseline" in captured["body"]
    # Pro Suite: the PDF carries the initial gap analysis + provisioning evidence.
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(captured["pdf"])).pages)
    assert "Initial Gap Analysis" in text
    assert "Multi-subsidiary" in text  # Pro-only provisioning row
