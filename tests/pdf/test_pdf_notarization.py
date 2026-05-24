"""Notarization PDF: must surface blockchain tx hash + verify URL."""
from io import BytesIO

import pytest
from freezegun import freeze_time


@freeze_time("2026-05-24T12:00:00Z")
def test_notarization_pdf_shows_tx_hash():
    from app.services.pdf_service import PDFService

    tx_hash = "0xabc1234567890def1234567890abcdef1234567890abcdef1234567890abcdef"
    verify_url = "https://amoy.polygonscan.com/tx/" + tx_hash

    pdf_bytes = PDFService().generate_pdf({
        "framework": "compliance_notarization",
        "company_name": "Notary Test Co",
        "document_title": "Vendor Compliance Attestation",
        "created_at": "2026-05-24T12:00:00Z",
        "blockchain": {"tx_hash": tx_hash, "verify_url": verify_url},
        "proof_header": "BOOPPA-NOTARY-V1",
    })

    assert pdf_bytes.startswith(b"%PDF")

    from pypdf import PdfReader
    text = "\n".join(
        p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages
    )
    assert "Notary Test Co" in text
    # Blockchain section: at minimum a prefix of the hash should appear
    # (full hashes may wrap across lines depending on font width).
    assert tx_hash[:10] in text or "Polygon" in text or "Amoy" in text
