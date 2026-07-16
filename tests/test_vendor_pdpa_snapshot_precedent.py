"""Phase 5A: the Quarterly PDPA Snapshot grounds worsened dimensions in real
PDPC enforcement precedent, reusing the curated `pdpc_precedents` register.

Only dimensions that map to a finding key with precedents on file surface a
line — a worsened dimension with no precedent must never invent one.
"""
import io

import pypdf

from app.services.vendor_pdpa_snapshot_generator import (
    generate_vendor_pdpa_snapshot_pdf,
    _dimension_precedent,
)


def _text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _flip(name, prev="PASS", now="FAIL"):
    return {
        "dimension_name": name,
        "previous_status": prev,
        "current_status": now,
        "previous_score": 80,
        "current_score": 20,
    }


def test_breach_dimension_surfaces_precedent():
    pdf = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Acme Pte Ltd",
        "current_score": 40,
        "previous_score": 70,
        "dimension_flips": [_flip("Data Breach Notification (§26B-D)")],
        "is_baseline": False,
    })
    txt = _text(pdf)
    assert "Regulatory precedent" in txt
    assert "PDPC has " in txt


def test_dimension_without_precedent_shows_no_line():
    pdf = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Acme Pte Ltd",
        "current_score": 40,
        "previous_score": 70,
        "dimension_flips": [_flip("Security HTTP Headers")],
        "is_baseline": False,
    })
    txt = _text(pdf)
    assert "Regulatory precedent" not in txt


def test_baseline_edition_has_no_precedent_lines():
    # No flips → no per-dimension precedent lines.
    pdf = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Acme Pte Ltd",
        "current_score": 55,
        "is_baseline": True,
        "findings_count": 3,
    })
    txt = _text(pdf)
    assert "Regulatory precedent" not in txt


def test_dimension_precedent_helper_maps_only_known_keys():
    assert _dimension_precedent("Data Breach Notification (§26B-D)")
    assert _dimension_precedent("Security HTTP Headers") is None
    assert _dimension_precedent("") is None
