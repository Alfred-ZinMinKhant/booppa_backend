"""PDPA Monitor month-over-month report deliverable (Phase E of the remediation).

Verifies the generator renders delta + baseline editions, and that the task
assembles current-vs-previous from the two latest scans, uploads, and emails.
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO

from pypdf import PdfReader

from app.services.regulatory_registry import BODY_PDPC, cite

_PDPA_PENALTY_CITATION = cite(BODY_PDPC, "pdpa_s48j")


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

    async def fake_email(self, to_email, subject, body_html, **kwargs):
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
    # The report PDF now ships as an email attachment (Fix 1), so the body
    # references the attached PDF rather than an expiring download link.
    assert "attached to this email" in captured["body"]
    # The report compares the two scans (61 current vs 54 previous).
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(captured["pdf"])).pages)
    assert "61" in txt and "54" in txt


def test_briefing_bullets_and_their_citations_reach_the_pdf():
    """The PDF is the artifact a customer forwards to a regulator.

    `_pdpa_monitor_briefing_bullets` — the function carrying the finding-grounded
    PDPA section citations — fed `briefing_html` in the email body and nothing
    else. The generator's data contract had no field for it, so a Monitor PDF
    contained zero §-references no matter what the findings said. A reviewer
    checking the deliverable for "a real §-number instead of (none provided)"
    found no citation at all, which is indistinguishable from the bug.
    """
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf

    pdf = generate_pdpa_monitor_report_pdf({
        "company_name": "Netpoleon",
        "current_score": 58,
        "previous_score": 58,
        "findings_count": 7,
        # A no-change month: no dimension moved, so this is exactly the edition
        # that previously rendered without a single section reference.
        "dimension_changes": [],
        "briefing_bullets": [
            "Publish a retention schedule (PDPA &sect;25) — 2 weeks.",
            "Document cross-border transfer safeguards (PDPA &sect;26) — 3 weeks.",
        ],
    })
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)

    assert "Regulatory Priorities" in txt
    assert "retention schedule" in txt
    # The citations themselves are the point of the check.
    assert "§25" in txt and "§26" in txt


def test_pdf_omits_the_briefing_section_when_there_are_no_bullets():
    """No heading over an empty space — and the caller may legitimately pass
    nothing (older editions, a generator failure upstream)."""
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf

    pdf = generate_pdpa_monitor_report_pdf({
        "company_name": "Netpoleon", "current_score": 58, "previous_score": 58,
    })
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Regulatory Priorities" not in txt


def test_bullets_are_not_double_escaped(test_db, mocker):
    """Every return path of `_pdpa_monitor_briefing_bullets` already runs `_xe`.

    Escaping again in the generator would print a literal "&amp;" to the
    customer — the "Q&A; Coverage" glitch, one layer further down.
    """
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf

    pdf = generate_pdpa_monitor_report_pdf({
        "company_name": "Netpoleon", "current_score": 58, "previous_score": 58,
        "briefing_bullets": ["Review Q&amp;A retention under &sect;25."],
    })
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Q&A retention" in txt
    assert "&amp;" not in txt


def test_urgent_findings_regulatory_text_30_plus_days():
    """Verify urgent box text cites Section 48J(6) of the PDPA for 30+ day open findings."""
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf

    pdf_30 = generate_pdpa_monitor_report_pdf({
        "company_name": "Netpoleon",
        "current_score": 58,
        "previous_score": 58,
        "urgent_findings": [
            {"label": "Security HTTP Headers", "days_open": 35, "severity": "HIGH"},
        ],
    })
    txt_30_clean = " ".join(("\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_30)).pages)).split())

    assert "URGENT — UNRESOLVED HIGH-RISK FINDINGS" in txt_30_clean
    assert "Security HTTP Headers" in txt_30_clean
    assert "35 days" in txt_30_clean
    # The statutory citation is sourced from the regulatory registry, not typed
    # here — assert against the registry so a correction to the row moves the
    # test with the document instead of pinning the old text.
    assert f"Under {_PDPA_PENALTY_CITATION}, PDPC must have regard to the duration of non-compliance" in txt_30_clean
    assert "typically begin with a review" not in txt_30_clean

    pdf_20 = generate_pdpa_monitor_report_pdf({
        "company_name": "Netpoleon",
        "current_score": 58,
        "previous_score": 58,
        "urgent_findings": [
            {"label": "Security HTTP Headers", "days_open": 20, "severity": "HIGH"},
        ],
    })
    txt_20_clean = " ".join(("\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_20)).pages)).split())
    assert "20 days" in txt_20_clean
    assert "Section 48J(6)" not in txt_20_clean

