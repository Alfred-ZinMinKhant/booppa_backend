"""Visual smoke render of the upgraded PDPA score table.

We don't byte-diff PDFs (reportlab font caching makes that brittle), but we
do extract text via pypdf and assert every new Tier 1-5 dimension surfaces in
the rendered output. This catches layout regressions the unit tests on the
Table object structure can't.

If you want to eyeball the artefact, run pytest with PDPA_KEEP_ARTEFACT=1 and
the PDF is written to /tmp/pdpa_smoke.pdf.
"""
import os
import re
from io import BytesIO

import pytest
from freezegun import freeze_time
from pypdf import PdfReader

from app.services.pdf_service import PDFService


def _normalise(s: str) -> str:
    """pypdf reconstructs cell text with line breaks where the PDF wraps. We
    collapse all whitespace so 'Third-Party Tracker Inventory' matches even
    when rendered across two lines."""
    return re.sub(r"\s+", " ", s)


_NEW_DIMENSIONS = [
    "NRIC Exposure",
    "Retention Limitation",          # §25
    "Data Breach Notification",      # §26B-D
    "Cross-Border Transfer",         # §26
    "Third-Party Tracker Inventory",
    "Cookie Consent Mechanism",
    "Privacy Policy",
]


def _realistic_scan_data() -> dict:
    """Mirrors what app/workers/tasks.py::_scan_site_metadata writes after
    Tiers 1-3 run. Mixes Compliant / Partial / Non-Compliant so the rendered
    cells exercise all severity colours."""
    return {
        "nric": {"status": "Non-Compliant", "score": 0, "kind": "leakage",
                 "items": [{"kind": "leakage", "snippet": "Customer file …",
                            "source_url": "https://acme.sg/cust", "confidence": 0.9,
                            "note": ""}]},
        "policy_clauses": {
            "status": "Partial", "score": 67,
            "present_count": 4, "total": 6,
            "missing": ["retention", "data_subject_rights"],
            "items": [{"clause": "retention", "present": False}],
        },
        "pdpc_enforcement": {"checked": True, "found": True,
                             "cases": [{"title": "Acme Pte Ltd fined S$5,000",
                                        "date": "", "url": "https://x"}]},
        "hosting": {"checked": True, "inferred_provider": "AWS",
                    "inferred_region": None},
        "ssl_grade": {"checked": True, "grade": "A"},
        "trackers": {"inventory": ["Google Analytics", "Meta Pixel"],
                     "pre_consent": [{"vendor": "GA", "sample_url": "x", "count": 3}],
                     "post_consent": [], "total_requests_captured": 17},
        "consent_mechanism": {"has_cookie_banner": True,
                              "detected_providers": ["onetrust"]},
        "privacy_policy": {"found": True, "link": "https://acme.sg/privacy"},
        "dpo_compliance": {"has_dpo": True, "dpo_email": "dpo@acme.sg"},
        "security_headers": {"hsts": True, "csp": False,
                             "x_content_type_options": True, "x_frame_options": True,
                             "referrer_policy": False, "permissions_policy": False},
        "primary_language": "en",
    }


@freeze_time("2026-06-04T10:00:00Z")
def test_pdpa_pdf_renders_all_upgraded_dimensions():
    scan_data = _realistic_scan_data()
    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Smoke Co",
        "created_at": "2026-06-04T10:00:00Z",
        "risk_score": 60,
        "findings": [],
        "scan_data": scan_data,
    })

    # 1. Structural: valid PDF, non-trivial size
    assert pdf_bytes.startswith(b"%PDF"), "not a PDF"
    assert len(pdf_bytes) > 3000, f"PDF suspiciously small: {len(pdf_bytes)} bytes"

    # 2. Textual: every new dimension surfaces somewhere in the rendered text
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    flat = _normalise(text)
    missing = [d for d in _NEW_DIMENSIONS if d not in flat]
    assert not missing, (
        f"PDPA PDF missing dimensions: {missing}\n"
        f"--- rendered text (first 800 chars) ---\n{text[:800]}"
    )

    # 3. Overall score row exists
    assert "Overall Score" in flat

    # 4. Company name renders
    assert "Acme Smoke Co" in flat

    # Optionally keep the artefact for manual inspection.
    if os.environ.get("PDPA_KEEP_ARTEFACT"):
        with open("/tmp/pdpa_smoke.pdf", "wb") as f:
            f.write(pdf_bytes)


@freeze_time("2026-06-04T10:00:00Z")
def test_pdpa_pdf_renders_remediation_history():
    """Tier 6: when the report carries `remediations`, the PDF shows them."""
    scan_data = _realistic_scan_data()
    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Remediator Co",
        "created_at": "2026-06-04T10:00:00Z",
        "risk_score": 30,
        "findings": [],
        "scan_data": scan_data,
        "remediations": [
            {
                "finding_key": "nric:collection",
                "label": "NRIC collection detected",
                "status": "fixed",
                "confirmation_status": "confirmed",
                "marked_at": "2026-05-15T09:00:00Z",
                "confirmed_at": "2026-06-01T09:00:00Z",
            },
            {
                "finding_key": "tracker:google_analytics",
                "label": "Pre-consent third-party tracker: Google Analytics",
                "status": "fixed",
                "confirmation_status": "regressed",
                "marked_at": "2026-05-20T09:00:00Z",
                "confirmed_at": None,
            },
            {
                "finding_key": "clause:retention",
                "label": "Privacy policy: Retention period missing",
                "status": "fixed",
                "confirmation_status": "pending",
                "marked_at": "2026-06-03T09:00:00Z",
                "confirmed_at": None,
            },
        ],
    })

    assert pdf_bytes.startswith(b"%PDF")
    flat = _normalise("\n".join(p.extract_text() or ""
                                for p in PdfReader(BytesIO(pdf_bytes)).pages))
    # Section header in the PDF is rendered uppercase by _section_header.
    assert "REMEDIATION STATUS" in flat.upper()
    # Confirmation badges (the leading glyphs may or may not extract; we
    # rely on the word).
    upper = flat.upper()
    assert "CONFIRMED" in upper
    assert "REGRESSED" in upper
    assert "PENDING" in upper
    assert "NRIC collection detected" in flat


@freeze_time("2026-06-04T10:00:00Z")
def test_pdpa_pdf_renders_clean_scan_without_crash():
    """A scan with no findings still produces a valid PDF and shows
    Compliant statuses for the new dimensions."""
    scan_data = {
        "nric": {"status": "Compliant", "score": 100, "kind": "none", "items": []},
        "policy_clauses": {"status": "Compliant", "score": 100,
                           "present_count": 6, "total": 6, "missing": [],
                           "items": [{"clause": "retention", "present": True}]},
        "pdpc_enforcement": {"checked": True, "found": False, "cases": []},
        "hosting": {"checked": True, "inferred_provider": "AWS",
                    "inferred_region": "Singapore"},
        "trackers": {"inventory": [], "pre_consent": [], "post_consent": []},
        "consent_mechanism": {"has_cookie_banner": True,
                              "detected_providers": ["onetrust"]},
        "primary_language": "en",
    }
    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Clean Co",
        "created_at": "2026-06-04T10:00:00Z",
        "risk_score": 5,
        "findings": [],
        "scan_data": scan_data,
    })

    assert pdf_bytes.startswith(b"%PDF")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    flat = _normalise(text)
    assert "Clean Co" in flat
    # The two highest-signal new dimensions should both appear
    assert "NRIC Exposure" in flat
    assert "Data Breach Notification" in flat
