"""Cover Sheet regression locks (CB-4 email leak, CB-5 blank RFP hash).

CB-4: the "Prepared for" field used to print our own harness default
      ``evidence@booppa.io`` instead of the paying customer's contact email.
CB-5: the RFP Complete Kit row in the Anchored Documents table showed a blank
      dash where its SHA-256 hash belonged, despite a valid tx hash beside it.

Both are content contracts on `generate_cover_sheet`, so we render the real PDF and
assert on its extracted text.
"""
from io import BytesIO

from app.services.cover_sheet_generator import generate_cover_sheet


def _extract_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


_BUYER_EMAIL = "buyer-contact@ensigninfosecurity.example"
_RFP_HASH = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"


def _cover_data():
    return {
        "report_id": "cover-123",
        "company_name": "Ensign InfoSecurity Pte Ltd",
        "customer_email": _BUYER_EMAIL,
        "pdpa_status": "completed",
        "pdpa_score": 61,
        "pdpa_details": {},
        "pdpa_tx_hash": None,
        "rfp_status": "completed",
        "rfp_details": {"product_type": "rfp_complete", "qa_count": 15},
        "rfp_tx_hash": None,
        "anchored_documents": [
            {
                "descriptor": "RFP Complete Kit",
                "filename": "RFP Complete Kit",
                "file_hash": _RFP_HASH,
                "tx_hash": None,
            },
        ],
    }


def test_prepared_for_shows_buyer_email_not_internal_default():
    pdf = generate_cover_sheet(_cover_data())
    assert pdf[:4] == b"%PDF"
    text = _extract_text(pdf)
    # The buyer's contact email must appear...
    assert _BUYER_EMAIL in text, "buyer email missing from cover sheet 'Prepared for'"
    # ...and our internal harness default must never leak onto a customer document.
    assert "evidence@booppa.io" not in text


def test_anchored_rfp_row_renders_its_hash_not_a_blank_dash():
    pdf = generate_cover_sheet(_cover_data())
    text = _extract_text(pdf)
    # The Anchored Documents table truncates the hash as <first18>…<last6>.
    head, tail = _RFP_HASH[:18], _RFP_HASH[-6:]
    assert head in text and tail in text, "RFP Complete Kit row is missing its SHA-256 hash"
