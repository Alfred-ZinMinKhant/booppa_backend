"""PDPA Monitor month-over-month report deliverable (Phase E of the remediation).

Verifies the generator renders delta + baseline editions, and that the task
assembles current-vs-previous from the two latest scans, uploads, and emails.
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO

from pypdf import PdfReader


def test_generator_delta_and_baseline():
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf

    delta = generate_pdpa_monitor_report_pdf({
        "company_name": "Crayon",
        "current_score": 61,
        "previous_score": 54,
        "findings_count": 4,
        "dimension_changes": [
            {"dimension_name": "Cookie Consent", "previous_status": "Partial", "current_status": "Non-Compliant"},
        ],
    })
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(delta)).pages)
    assert "PDPA Monitor Report" in txt
    assert "Cookie Consent" in txt and "61" in txt and "54" in txt

    baseline = generate_pdpa_monitor_report_pdf({"company_name": "Crayon", "current_score": 54, "previous_score": None})
    btxt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(baseline)).pages)
    assert "first monitoring cycle" in btxt.lower() or "baseline" in btxt.lower()


def test_task_builds_delta_from_two_reports(test_db, mocker):
    from app.core.models import User, Report

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
        email="monitor+report@booppa.io",
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="pdpa_monitor",
        company="Crayon Singapore",
        website="https://crayon.com/sg/",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    now = datetime.now(timezone.utc)
    # Previous scan (compliance 54), then current (compliance 61).
    test_db.add(Report(
        owner_id=user.id, framework="pdpa_quick_scan", company_name="Crayon Singapore",
        company_website="https://crayon.com/sg/", status="completed",
        assessment_data={"compliance_score": 54}, completed_at=now - timedelta(days=30),
    ))
    test_db.add(Report(
        owner_id=user.id, framework="pdpa_quick_scan", company_name="Crayon Singapore",
        company_website="https://crayon.com/sg/", status="completed",
        assessment_data={"compliance_score": 61, "display_url": "https://crayon.com/sg/",
                         "detailed_findings": [{"severity": "high"}, {"severity": "low"}]},
        completed_at=now,
    ))
    test_db.commit()

    from app.workers.tasks import run_pdpa_monitor_report_for_user
    run_pdpa_monitor_report_for_user(str(user.id), user.email)

    assert captured.get("pdf", b"").startswith(b"%PDF")
    assert captured["to"] == "monitor+report@booppa.io"
    assert "Monitor Report" in captured["subject"]
    assert "Download your Monitor Report" in captured["body"]
    # The report compares the two scans (61 current vs 54 previous).
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(captured["pdf"])).pages)
    assert "61" in txt and "54" in txt
