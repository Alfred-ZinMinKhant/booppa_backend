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


# ── PDPA report: summary table and "How to Verify" must agree ─────────────────
# Regression: _blockchain_block gated tx_hash through is_real_onchain_tx (so the
# summary table showed "—"), but _how_to_verify_block seven lines later did not,
# so Step 4 of the delivered PDF told the buyer to search a `demo-0x…` hash on
# Polygonscan. Both must be "—" or both the same real tx, never one of each.

def _flat_text(flowables):
    """Flatten Paragraph text out of a flowable tree (tables nest their cells)."""
    out = []
    for f in flowables:
        if hasattr(f, "text"):
            out.append(f.text)
        for row in getattr(f, "_cellvalues", []) or []:
            for cell in row:
                out.extend(_flat_text(cell if isinstance(cell, list) else [cell]))
    return out


def _pdpa_tx_pair(tx):
    from app.services.pdf_service import PDFService

    svc = PDFService()
    data = {"tx_hash": tx, "audit_hash": "e3b0c442", "report_id": "r-1"}
    summary = _flat_text(svc._blockchain_block(data))
    step4 = " ".join(_flat_text(svc._how_to_verify_block(data)))
    return summary[summary.index("TRANSACTION HASH") + 1], step4


def test_pdpa_verify_steps_gate_demo_tx_like_the_summary_table():
    for bad in ("demo-0x" + "df" * 32, "PENDING", None, "cs_test_abc123"):
        summary_tx, step4 = _pdpa_tx_pair(bad)
        assert summary_tx == "—"
        assert str(bad) not in step4
        assert "polygonscan" not in step4.lower()
        assert "not yet been anchored" in step4


def test_pdpa_verify_steps_show_a_real_tx():
    summary_tx, step4 = _pdpa_tx_pair(_REAL_TX)
    assert summary_tx == _REAL_TX
    assert _REAL_TX in step4
