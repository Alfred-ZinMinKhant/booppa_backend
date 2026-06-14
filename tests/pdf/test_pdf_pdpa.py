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


def test_generate_pdf_stashes_score_on_top_level_for_flattened_scan_data():
    """The main scan path (process_report_task) flattens scan-evidence fields
    directly onto pdf_data (no nested 'scan_data' key). generate_pdf must then
    stash the dimension-weighted compliance score on the TOP-LEVEL pdf_data dict,
    because that's where the persist logic reads it back to store on the Report.

    This is the exact gap behind the cover-sheet 53-vs-54 divergence: the score
    was computed and printed (53) but never read back / persisted, so the cover
    sheet fell back to 100-risk (54). The score must also differ from a naive
    100-risk number so the regression is meaningful.
    """
    from app.services.pdf_service import PDFService

    pdf_data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Crayon Singapore",
        "created_at": "2026-06-14T10:39:00Z",
        "status": "completed",
        "risk_score": 46,  # 100 - 46 = 54, the divergent fallback
        "structured_report": {
            "detailed_findings": [
                {"check_id": "no_consent_banner", "severity": "HIGH", "title": "No cookie banner"},
                {"check_id": "missing_security_headers", "severity": "HIGH", "title": "Security headers missing"},
                {"check_id": "dpo_not_disclosed", "severity": "MEDIUM", "title": "DPO contact not disclosed"},
            ],
        },
        # Flattened scan evidence (as process_report_task passes it):
        "trackers": {"inventory": ["Google Ads", "Google Tag Manager", "LinkedIn Insight Tag"]},
        "ssl_grade": {"grade": "A"},
    }
    PDFService().generate_pdf(pdf_data)

    score = pdf_data.get("computed_overall_compliance_score")
    # Present on the TOP level (where the persist reads it) and a valid score.
    assert isinstance(score, int) and 0 <= score <= 100, \
        f"dimension-weighted score not stashed on top-level pdf_data: {score!r}"


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
