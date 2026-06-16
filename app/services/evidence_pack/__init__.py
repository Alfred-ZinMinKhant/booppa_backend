"""PDPA Compliance Evidence Pack (BCEP) — ported into the Booppa app.

Generates 7 PDPA governance documents (DPMP, ROPA, Data Inventory, Vendor/DPA
Register, Breach Runbook, Training Register, Security Review Log), anchors their
hashes on the existing chain (Amoy testnet under Lean Mode), and renders branded
DRAFT PDFs the client must verify + sign before evidentiary use.

Differences from the upstream BCEP-v1.1 drop:
  * LLM calls route through the existing DeepSeek configuration (settings), not a
    raw OpenAI client + os.environ.
  * Anchoring reuses BlockchainService (Amoy testnet); the mainnet-only
    polygon_anchor module is NOT ported. PDFs disclose the testnet honestly.
  * The fabricated Booppa UEN (202415732W) is stripped everywhere; the CUSTOMER's
    UEN (from intake) is what appears on their documents.
"""

from .document_generator import generate_evidence_pack, generate_document  # noqa: F401
from .pdf_builder import build_evidence_pack_pdfs, build_single_pdf, DOC_META  # noqa: F401
