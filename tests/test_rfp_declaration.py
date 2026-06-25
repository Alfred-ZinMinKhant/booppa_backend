"""RFP Supplier Compliance Declaration generator (Sprint 5c)."""
from app.services.rfp_declaration_generator import build_supplier_declaration_pdf


def _extract_text(pdf_bytes: bytes) -> str:
    """Best-effort text extraction; falls back to raw bytes if no PDF lib."""
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return pdf_bytes.decode("latin-1", errors="ignore")


def test_declaration_is_valid_pdf_with_all_items():
    pdf = build_supplier_declaration_pdf(
        company_name="Acme Pte Ltd",
        vendor_ctx={"uen": "201912345A", "acra_name": "Acme Pte Ltd"},
        acra_live={"found": True, "entity_status": "Live"},
        compliance_score=78,
        report_id="rep-123",
    )
    assert pdf[:4] == b"%PDF"
    text = _extract_text(pdf)
    for code in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert code in text, f"missing {code}"


def test_d4_verified_when_score_present():
    pdf = build_supplier_declaration_pdf(
        company_name="Acme Pte Ltd",
        acra_live={"found": True, "entity_status": "Live"},
        compliance_score=82,
    )
    text = _extract_text(pdf)
    assert "82/100" in text
    assert "VERIFIED" in text


def test_d3_never_claims_full_verification():
    """Debarment must remain client-declared; ACRA is corroboration only."""
    pdf = build_supplier_declaration_pdf(
        company_name="Acme Pte Ltd",
        acra_live={"found": True, "entity_status": "Live"},
        compliance_score=70,
    )
    text = _extract_text(pdf).lower()
    assert "debarment" in text
    # The honesty caveat must be present.
    assert "debarment register" in text


def test_xml_escape_company_name_does_not_crash():
    pdf = build_supplier_declaration_pdf(
        company_name="Smith & Jones <Holdings>",
        acra_live={"found": False},
    )
    assert pdf[:4] == b"%PDF"


def test_returns_pdf_even_without_score():
    pdf = build_supplier_declaration_pdf(company_name="NoScore Pte Ltd")
    assert pdf[:4] == b"%PDF"
    text = _extract_text(pdf)
    assert "PDPA" in text
