"""PDPA Quick Scan PDF content checks.

PDFs are non-deterministic across pdf-lib versions, so we parse with pypdf and
assert the rendered TEXT contains the company name and framework heading rather
than diffing bytes.
"""
from io import BytesIO

import pytest
from freezegun import freeze_time


@freeze_time("2026-05-24T12:00:00Z")
def test_pdpa_quick_scan_pdf_has_company_and_framework():
    from app.services.pdf_service import PDFService

    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 42,
        "findings": [
            {"category": "Cookie consent", "severity": "high", "summary": "no banner"},
        ],
    })

    assert pdf_bytes.startswith(b"%PDF"), "not a PDF"
    assert len(pdf_bytes) > 1500, "PDF suspiciously small"

    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    assert "Acme Test Co" in text
    # Framework label is rendered title-cased ("Pdpa Quick Scan") on the cover.
    assert "Pdpa Quick Scan" in text or "PDPA QUICK SCAN" in text.upper()


def test_compliance_score_table_stashes_overall_for_persistence():
    """`_compliance_score_table` must write the headline compliance score back
    into the scan_data dict so the PDPA fulfillment can persist it and the
    Compliance Evidence Cover Sheet can display the identical number (verbatim)
    instead of recomputing and drifting (53-vs-54 audit finding)."""
    from app.services.pdf_service import PDFService

    scan_data = {"some": "signals"}
    findings = [
        {"check_id": "no_consent_banner", "severity": "HIGH", "title": "No cookie banner"},
        {"check_id": "no_privacy_policy", "severity": "HIGH", "title": "No privacy policy"},
    ]
    PDFService()._compliance_score_table(findings, scan_data=scan_data)

    score = scan_data.get("computed_overall_compliance_score")
    assert isinstance(score, int) and 0 <= score <= 100, \
        f"overall compliance score not stashed for persistence: {score!r}"


@freeze_time("2026-05-24T12:00:00Z")
def test_pdpa_pdf_is_deterministic_when_time_frozen():
    """Two generations with identical input + frozen clock should match by length
    (full byte equality is fragile across reportlab font caches, but length is
    a useful regression guardrail)."""
    from app.services.pdf_service import PDFService

    data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Deterministic Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 10,
        "findings": [],
    }
    a = PDFService().generate_pdf(data)
    b = PDFService().generate_pdf(data)
    assert abs(len(a) - len(b)) < 50, "PDF output drift > 50 bytes — investigate non-determinism"
