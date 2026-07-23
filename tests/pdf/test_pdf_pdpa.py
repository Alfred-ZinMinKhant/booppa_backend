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
    # A one-time PDPA scan (no Monitor trigger source) is branded "PDPA Snapshot"
    # on both the cover subtitle and the running header band — never a plain
    # "Quick Scan". A Monitor rescan (triggered_by=pdpa_monitor_*) reads "PDPA Monitor".
    assert "PDPA SNAPSHOT" in text.upper()
    assert "QUICK SCAN" not in text.upper()


@pytest.mark.parametrize(
    "triggered_by, expect_label",
    [
        ("pdpa_monitor_monthly", "PDPA MONITOR"),
        ("pdpa_monitor_annual", "PDPA MONITOR"),
        ("vendor_pro", "PDPA SNAPSHOT"),
        (None, "PDPA SNAPSHOT"),
    ],
)
def test_pdpa_pdf_branding_by_trigger_source(triggered_by, expect_label):
    """The underlying pdpa_quick_scan PDF is branded by its trigger source so a
    PDPA Monitor subscriber's deliverable never reads as a plain Quick Scan, and
    a Vendor Pro / one-time scan is not mislabeled as Monitor. Regression guard
    for the billing-integrity report: assessment_data['triggered_by'] must reach
    the PDF (single_products._fulfill_pdpa) and drive both the cover subtitle and
    the running header band (pdf_service.generate_pdf)."""
    from app.services.pdf_service import PDFService
    from pypdf import PdfReader

    data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 42,
        "findings": [],
    }
    if triggered_by is not None:
        data["assessment_data"] = {"triggered_by": triggered_by}

    pdf_bytes = PDFService().generate_pdf(data)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages).upper()

    # The deliverable's label surfaces (cover subtitle + running header band)
    # carry expect_label; a plain "Quick Scan" must never appear.
    assert expect_label in text
    assert "QUICK SCAN" not in text
    # A non-Monitor scan must never be branded "PDPA Monitor". (The reverse isn't
    # asserted: a Monitor PDF legitimately uses the word "snapshot" in generic
    # descriptive prose, e.g. "this snapshot has the following limitations".)
    if expect_label != "PDPA MONITOR":
        assert "PDPA MONITOR" not in text


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


@freeze_time("2026-05-24T12:00:00Z")
def test_unassessed_dimensions_excluded_from_overall_not_fabricated():
    """Dimensions whose underlying check did not run (retention with no clause
    classifier, breach with no PDPC check, cross-border with no hosting check,
    tracker with no rendered scan) must render N/A and be EXCLUDED from the
    weighted overall — never a fabricated 70/75 folded into the headline number.

    With no findings, every dimension that *was* assessed is clean (>=90). If the
    four un-assessed dimensions were still scored 70/75 the overall would be
    dragged into the ~80s; excluding them keeps it >=90. That gap is the test."""
    from app.services.pdf_service import PDFService

    # scan_data deliberately omits policy_clauses / hosting / pdpc_enforcement /
    # trackers so those four dimensions hit the "did not run" branch → N/A.
    scan_data = {"company_name": "NA Test Co"}
    PDFService()._compliance_score_table([], scan_data=scan_data)
    overall = scan_data.get("computed_overall_compliance_score")
    assert isinstance(overall, int)
    assert overall >= 90, (
        f"overall={overall}: un-assessed dimensions appear to be dragging the "
        f"headline number — they must be excluded, not scored 70/75"
    )


def _cell_texts(table):
    """Flatten a reportlab Table's cell Paragraphs to their source text."""
    out = []
    for row in getattr(table, "_cellvalues", []):
        cells = []
        for cell in row:
            txt = getattr(cell, "text", None)
            cells.append(txt if txt is not None else str(cell))
        out.append(cells)
    return out


def test_unassessed_retention_renders_na_not_fabricated_score():
    """The retention row must show N/A (not a fabricated '70/100 Partial') when
    the policy clause classifier did not run. Asserts on the rendered table cells
    directly — robust to PDF pagination / text-extraction quirks."""
    from app.services.pdf_service import PDFService

    # No policy_clauses / hosting / pdpc_enforcement / trackers → those four
    # dimensions are un-assessed.
    table = PDFService()._compliance_score_table([], scan_data={"company_name": "NA Co"})
    rows = _cell_texts(table)

    retention_rows = [r for r in rows if r and "Retention Limitation" in r[0]]
    assert retention_rows, "retention dimension row not found"
    score_cell, status_cell = retention_rows[0][1], retention_rows[0][2]
    assert "N/A" in score_cell, f"retention score should be N/A, got {score_cell!r}"
    assert "70/100" not in score_cell, "fabricated 70/100 still rendered"
    assert "N/A" in status_cell, f"retention status should be N/A, got {status_cell!r}"


@freeze_time("2026-05-24T12:00:00Z")
def test_positive_verdicts_carry_a_provenance_qualifier():
    """Layer-3: a "Compliant" dimension is an inference from public disclosure,
    not an audit finding. The report must say so, or a positive verdict reads
    as an assurance we never performed."""
    from app.services.pdf_service import PDFService
    from pypdf import PdfReader

    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 12,
        "findings": [],
    })
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    text = " ".join(raw.split())

    assert "Basis: automated public-site scan on 2026-05-24" in text
    assert "not an audit of internal controls" in text
