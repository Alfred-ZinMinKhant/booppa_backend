"""Regressions for defects the layout refresh's visual review turned up in
``pdf_service``. Both were invisible to content-assertion tests on small
fixtures, which is why the sample-rendering loop exists.
"""
from io import BytesIO

import pytest
from pypdf import PdfReader

from app.services import pdf_service
from app.services.pdf_service import PDFService, _as_list


# ── bullet-list fields ─────────────────────────────────────────────────────────

def test_as_list_splits_a_string_instead_of_iterating_its_characters():
    """A string field rendered one bullet per LETTER — "• T / • h / • e" — and
    inflated a 14-page report to 83 pages."""
    assert _as_list("Deploy a consent banner") == ["Deploy a consent banner"]


def test_as_list_splits_multiline_strings_into_bullets():
    assert _as_list("- first\n- second\n") == ["first", "second"]


@pytest.mark.parametrize("value,expected", [
    (None, []), ("", []), ([], []),
    (["a", "b"], ["a", "b"]),
    (["a", "", None], ["a"]),
])
def test_as_list_edge_cases(value, expected):
    assert _as_list(value) == expected


def test_string_acceptance_criteria_does_not_explode_the_page_count():
    finding = {
        "title": "Unconsented tracker",
        "severity": "HIGH",
        "acceptance_criteria": "Consent banner blocks all non-essential trackers "
                               "until the visitor opts in.",
        "requirements": "Deploy the banner on every page.",
        "recommended_tools": "Klaro, Osano",
    }
    block = PDFService()._task_block(1, finding)
    # One paragraph per field, not one per character.
    assert len(block) < 20


# ── page furniture ─────────────────────────────────────────────────────────────

def _pdpa_pdf(findings):
    return PDFService().generate_pdf({
        "report_id": "RPT-TEST",
        "company_name": "Ampersand & Sons Pte Ltd",
        "website_url": "https://example.test",
        "framework": "PDPA",
        "report_type": "pdpa_quick_scan",
        "structured_report": {"detailed_findings": findings},
        "scan_data": {"company_name": "Ampersand & Sons Pte Ltd", "findings": findings},
    })


def test_pdpa_report_numbers_every_page_with_a_total():
    text = "".join(
        (p.extract_text() or "") for p in PdfReader(BytesIO(_pdpa_pdf([]))).pages
    )
    assert "of" in text and "Page 1 of" in text


def test_pdpa_report_has_no_near_blank_pages():
    """The three unconditional PageBreaks reliably stranded pages holding only a
    section heading. Nothing but the running header/footer is a blank page."""
    reader = PdfReader(BytesIO(_pdpa_pdf([])))
    furniture = len("PDPA SNAPSHOT Booppa Smart Care LLC booppa.io Confidential Page of".split())
    for i, page in enumerate(reader.pages, 1):
        words = len((page.extract_text() or "").split())
        assert words > furniture + 25, f"page {i} holds almost nothing ({words} words)"


def test_company_name_with_ampersand_survives_to_the_page():
    text = "".join(
        (p.extract_text() or "") for p in PdfReader(BytesIO(_pdpa_pdf([]))).pages
    )
    assert "Ampersand & Sons Pte Ltd" in text
