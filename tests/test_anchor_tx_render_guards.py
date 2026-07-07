"""Regression guards (GTM readiness verification, Section C gaps 1 & 2).

A customer-facing document must never render a value that is not a real,
confirmed on-chain transaction as if it were one:

  * the due-diligence certificate must not print a demo/test-checkout hash
    (`demo_tx_hash`, shape-valid but `anchored=False`) as a "Transaction reference";
  * the quarterly PDPA snapshot must gate its `anchor_tx` line on a real tx,
    like every other anchor renderer (previously it rendered any truthy value).
"""
import io

import pypdf

from app.services.supplier_due_diligence_generator import (
    generate_certificate_pdf,
    demo_tx_hash,
)
from app.services.vendor_pdpa_snapshot_generator import (
    generate_vendor_pdpa_snapshot_pdf,
)


def _text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# A genuinely-shaped (but arbitrary) on-chain tx hash: 0x + 64 hex chars.
_REAL_TX = "0x" + "ab" * 32


def test_demo_hash_not_rendered_as_transaction_reference():
    fake = demo_tx_hash("evidence-abc")
    pdf = generate_certificate_pdf({
        "supplier_name": "Acme Pte Ltd",
        "buyer_company": "Buyer Org",
        "is_certificate": True,
        "resolved": True,
        "tx_hash": fake,       # shape-valid, but never mined
        "anchored": False,     # demo mode
    })
    txt = _text(pdf)
    assert fake not in txt, "demo hash must not appear in the certificate"
    assert "Transaction reference" not in txt


def test_real_anchored_tx_is_rendered():
    pdf = generate_certificate_pdf({
        "supplier_name": "Acme Pte Ltd",
        "buyer_company": "Buyer Org",
        "is_certificate": True,
        "resolved": True,
        "tx_hash": _REAL_TX,
        "anchored": True,
    })
    txt = _text(pdf)
    assert "anchored on Polygon Amoy" in txt


def test_snapshot_pending_anchor_tx_not_rendered():
    pdf = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Acme Pte Ltd",
        "current_score": 70,
        "previous_score": 70,
        "is_baseline": True,
        "anchor_tx": "PENDING",   # sentinel, not a real tx
    })
    txt = _text(pdf)
    assert "PENDING" not in txt
    assert "Integrity anchor" not in txt


def test_snapshot_real_anchor_tx_is_rendered():
    pdf = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Acme Pte Ltd",
        "current_score": 70,
        "previous_score": 70,
        "is_baseline": True,
        "anchor_tx": _REAL_TX,
    })
    txt = _text(pdf)
    assert "Integrity anchor" in txt


def test_raw_txn_prefers_snake_case_and_falls_back():
    """web3 >=6 exposes raw_transaction; the helper must use it, and still
    fall back to the legacy rawTransaction on older web3 (regression: 7.16
    broke every real anchor because the code used the removed camelCase name)."""
    from app.services.blockchain import _raw_txn

    class NewStyle:  # web3 >= 6
        raw_transaction = b"new-bytes"

    class OldStyle:  # web3 < 6
        rawTransaction = b"old-bytes"

    assert _raw_txn(NewStyle()) == b"new-bytes"
    assert _raw_txn(OldStyle()) == b"old-bytes"
