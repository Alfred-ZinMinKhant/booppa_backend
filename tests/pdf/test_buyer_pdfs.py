"""Buyer proof-of-value deliverable PDFs render as valid, non-empty documents.

These generators are the artifacts customers actually receive (Due-Diligence
Certificate, Verification Snapshot, Procurement Intelligence Report). They were
shipped without render coverage; this locks in that each renders a real PDF,
degrades gracefully on missing data, and escapes user-supplied strings.
"""
from io import BytesIO

from pypdf import PdfReader

from app.services.supplier_due_diligence_generator import (
    generate_certificate_pdf,
    demo_tx_hash,
    evidence_hash_for,
)
from app.services.buyer_procurement_report_generator import (
    generate_buyer_procurement_report_pdf,
)


def _text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)


# ── Due-Diligence Certificate / Verification Snapshot ────────────────────────

def test_certificate_pdf_renders_anchored():
    pdf = generate_certificate_pdf({
        "supplier_name": "Crayon Singapore Pte Ltd",
        "buyer_company": "Acme Procurement",
        "resolved": True,
        "risk_signal": "LOW RISK",
        "trust_score": 78,
        "compliance_score": 71,
        "trust_delta": 3,
        "is_certificate": True,
        "tx_hash": "0x" + "ab" * 32,
        "anchored": True,
    })
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "Supplier Due-Diligence Certificate" in text
    assert "Crayon Singapore" in text
    assert "78" in text


def test_snapshot_pdf_renders_unresolved():
    """is_certificate False + no scores → snapshot title, no crash on None scores."""
    pdf = generate_certificate_pdf({
        "supplier_name": "Unclaimed Vendor",
        "resolved": False,
        "is_certificate": False,
    })
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "Supplier Verification Snapshot" in text
    assert "UNRATED" in text


def test_certificate_pdf_escapes_xml_in_supplier_name():
    """A supplier name with & / < must not break ReportLab's Paragraph mini-XML."""
    pdf = generate_certificate_pdf({
        "supplier_name": "A & B <Holdings> Pte",
        "buyer_company": "Buyer & Co <Ltd>",
        "notes": "Reviewed <urgently> & flagged",
        "is_certificate": True,
        "tx_hash": "0x" + "cd" * 32,
        "anchored": False,
    })
    assert pdf.startswith(b"%PDF")  # renders without an unescaped-entity ValueError


# ── Procurement Intelligence Report ──────────────────────────────────────────

def test_procurement_report_renders_with_suppliers():
    pdf = generate_buyer_procurement_report_pdf({
        "company_name": "Acme Procurement",
        "plan_label": "Buyer Pro",
        "tier": "pro",
        "watchlist_summary": {"total": 2, "alerting": 1, "slipped": 1,
                              "alerting_names": ["Risky Vendor"]},
        "watched_suppliers": [
            {"vendor_name": "Good Vendor", "resolved": True,
             "risk_signal": "LOW RISK", "trust_score": 80, "trust_delta": 2},
            {"vendor_name": "Risky Vendor", "resolved": True,
             "risk_signal": "FLAGGED", "trust_score": 41, "trust_delta": -12},
        ],
        "tender_matches": [
            {"title": "Supply of IT Services", "agency": "GovTech", "closing_date": None},
        ],
    })
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "Procurement Intelligence Report" in text
    assert "Acme Procurement" in text


def test_procurement_report_renders_empty_watchlist():
    """Starter/no-org buyers with an empty estate still get a valid PDF."""
    pdf = generate_buyer_procurement_report_pdf({
        "company_name": "Solo Buyer",
        "plan_label": "Buyer Pro",
        "tier": "pro",
        "watchlist_summary": {},
        "watched_suppliers": [],
        "tender_matches": [],
    })
    assert pdf.startswith(b"%PDF")
    assert "Procurement Intelligence Report" in _text(pdf)


def test_procurement_report_escapes_xml():
    pdf = generate_buyer_procurement_report_pdf({
        "company_name": "Buyer & Sons <Pte>",
        "watched_suppliers": [{"vendor_name": "V & W <Ltd>", "resolved": True,
                               "trust_score": 50}],
    })
    assert pdf.startswith(b"%PDF")


# ── Demo/test-checkout anchoring is gas-free and deterministic ───────────────

def test_demo_tx_hash_is_deterministic_and_offchain():
    pdf = generate_certificate_pdf({"supplier_name": "X", "is_certificate": True})
    ev_hash = evidence_hash_for(pdf)
    assert len(ev_hash) == 64  # sha-256 hex

    h1 = demo_tx_hash(ev_hash)
    h2 = demo_tx_hash(ev_hash)
    assert h1 == h2                       # deterministic
    assert h1.startswith("0x") and len(h1) == 66
    assert h1 != demo_tx_hash(ev_hash[::-1])  # varies with input
