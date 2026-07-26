"""The Evidence Pack cover page must never present a non-chain value as proof.

Two regressions this pins:

1. `demo_tx_hash` (admin test checkout) used to return a bare "0x" + 64 hex
   value. `is_real_onchain_tx` is a SHAPE check, so it accepted that, and the
   cover page rendered "ANCHORED" plus a QR to a transaction that exists on no
   chain. A demo run must read PENDING.
2. The network name was hardcoded to "Polygon Amoy testnet", so after the
   mainnet switch the PDF would link to polygonscan.com while still claiming
   testnet. It must follow USE_MAINNET.
"""
from reportlab.platypus import Paragraph, Table

from app.core.config import settings
from app.services.evidence_pack.pdf_builder import DOC_META as _ALL_DOC_META, _cover_page
from app.services.supplier_due_diligence_generator import demo_tx_hash

DOC_TYPE = "dpmp"
# Use the real metadata — _cover_page reads number/ref/title off it.
DOC_META = _ALL_DOC_META[DOC_TYPE]


def _pack(tx_hash):
    return {
        "organisation": "Acme Pte Ltd",
        "pack_id": "pack-test-001",
        "framework": "PDPA",
        "generated_at": "2026-07-26T00:00:00",
        "hashes": {DOC_TYPE: "a" * 64},
        "anchoring": {DOC_TYPE: {"tx_hash": tx_hash, "anchor_time_utc": "2026-07-26T00:00:00"}},
    }


def _text_of(flowables) -> str:
    """Flatten every Paragraph/Table cell in the cover page into one string."""
    out = []
    for f in flowables:
        if isinstance(f, Paragraph):
            out.append(f.getPlainText())
        elif isinstance(f, Table):
            for row in f._cellvalues:
                for cell in row:
                    if isinstance(cell, Paragraph):
                        out.append(cell.getPlainText())
    return " | ".join(out)


def test_demo_hash_renders_pending_not_anchored():
    text = _text_of(_cover_page(_pack(demo_tx_hash("a" * 64)), DOC_META))

    assert "PENDING" in text
    assert "ANCHORED" not in text
    # The fake hash itself must not appear anywhere on the page.
    assert demo_tx_hash("a" * 64) not in text


def test_real_hash_renders_anchored_with_active_network_name():
    real_tx = "0x" + "b" * 64
    text = _text_of(_cover_page(_pack(real_tx), DOC_META))

    assert "ANCHORED" in text
    assert real_tx in text
    # Network label follows USE_MAINNET rather than a hardcoded "Amoy testnet".
    assert settings.active_polygon_network_name in text


def test_missing_anchor_renders_pending():
    text = _text_of(_cover_page(_pack(None), DOC_META))
    assert "PENDING" in text
    assert "ANCHORED" not in text
