"""Phase 5D: the Compliance Evidence Pack cover sheet's "PDPA Snapshot — Full
Report" section must clarify that "Full Report" is the untruncated presentation
of the SAME automated scan as the standalone PDPA Quick Scan — not a deeper scan.

A tester read "Full Report" as implying a broader/deeper crawl than the Quick
Scan; the clarifier removes that ambiguity. Any visible-structure change bumps
COVER_SHEET_SCHEMA_VERSION so existing customers are offered a free regenerate.
"""
import io

import pypdf

from app.services.cover_sheet_generator import (
    generate_cover_sheet,
    COVER_SHEET_SCHEMA_VERSION,
)


def _text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _gen() -> bytes:
    return generate_cover_sheet({
        "company_name": "Acme Pte Ltd",
        "pdpa_status": "Complete",
        "pdpa_score": 62,
        "pdpa_details": {
            "website_url": "https://acme.example",
            "total_findings": 2,
            "findings": [],
        },
    })


def test_full_report_clarifier_present():
    txt = _text(_gen())
    # The disambiguating phrasing: same scan as the Quick Scan, not deeper.
    assert "same automated scan" in txt
    assert "PDPA Quick Scan" in txt
    assert "not a deeper scan" in txt


def test_schema_version_bumped_past_v12():
    # v13 introduced the clarifier — the constant must move so existing
    # customers holding older copies are flagged for a free regenerate.
    assert COVER_SHEET_SCHEMA_VERSION >= 13
