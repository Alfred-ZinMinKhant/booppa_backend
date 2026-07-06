"""Phase 5C: RFP Complete personalization to a specific tender.

The kit attributes itself to the tender the buyer is bidding on (reference
number / issuing agency / title) when those identifiers are on file, and stays
generic when they are not — never inventing a tender.

Two layers: the pure attribution formatter (`_tender_attribution`) and the DOCX
render that surfaces it.
"""
import io

from docx import Document

from app.services.rfp_express_builder import RFPExpressBuilder


def _docx_text(pdf_bytes: bytes) -> str:
    doc = Document(io.BytesIO(pdf_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


# ── pure attribution ─────────────────────────────────────────────────────────

def test_attribution_full():
    line = RFPExpressBuilder._tender_attribution({
        "tender_ref": "GEBIZ/2026/T012",
        "tender_agency": "Ministry of Digital Development",
        "tender_title": "Cloud HR Platform",
    })
    assert "Tender GEBIZ/2026/T012" in line
    assert "issued by Ministry of Digital Development" in line
    assert "Cloud HR Platform" in line


def test_attribution_ref_only():
    line = RFPExpressBuilder._tender_attribution({"tender_ref": "T99"})
    assert line == "Tender T99"


def test_attribution_empty_when_no_tender_fields():
    assert RFPExpressBuilder._tender_attribution({}) == ""
    assert RFPExpressBuilder._tender_attribution(None) == ""
    assert RFPExpressBuilder._tender_attribution({"tender_ref": "  "}) == ""


# ── DOCX rendering ───────────────────────────────────────────────────────────

def test_docx_renders_tender_line_when_present():
    b = RFPExpressBuilder(vendor_id="v1", vendor_email="v@x.co")
    docx_bytes = b._build_docx(
        company_name="Acme Pte Ltd",
        vendor_url="https://acme.example",
        qa_answers={"data_protection": "We encrypt at rest."},
        ctx={"uen": "201812345A"},
        intake={"tender_ref": "GEBIZ/2026/T012", "tender_agency": "MDDI"},
    )
    txt = _docx_text(docx_bytes)
    assert "Prepared in response to: Tender GEBIZ/2026/T012" in txt
    assert "MDDI" in txt


def test_docx_omits_tender_line_when_absent():
    b = RFPExpressBuilder(vendor_id="v1", vendor_email="v@x.co")
    docx_bytes = b._build_docx(
        company_name="Acme Pte Ltd",
        vendor_url="https://acme.example",
        qa_answers={"data_protection": "We encrypt at rest."},
        ctx={"uen": "201812345A"},
        intake={},
    )
    txt = _docx_text(docx_bytes)
    assert "Prepared in response to" not in txt
