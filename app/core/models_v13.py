"""
Booppa V13 — PDPA Compliance Evidence Pack (BCEP)
=================================================
The `compliance_evidence_pack` SKU now generates the BCEP 7-document governance
pack (DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook, Training
Register, Security Review Log) — closing PDPC Levels 2-6 — instead of the old
cover-sheet-only flow.

One `EvidencePack` row per purchase. Lifecycle:
  queued → intake_pending → generating → anchoring → building_pdfs → ready | error

Documents/hashes/anchoring/download_urls are JSON blobs keyed by doc_type. Every
document is an AI-generated DRAFT with no evidentiary value until the client
verifies + signs it (the PDF carries that disclaimer).
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.core.db import Base


class EvidencePack(Base):
    __tablename__ = "evidence_packs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id = Column(String(120), nullable=False, unique=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(255), nullable=True, index=True)

    status = Column(String(32), nullable=False, default="queued", server_default="queued")
    organisation = Column(String(255), nullable=True)

    intake = Column(JSONB, nullable=True)          # structured intake form
    scan_evidence = Column(JSONB, nullable=True)   # observed website/PDPA-scan signals used to ground docs
    documents = Column(JSONB, nullable=True)       # {doc_type: doc_json}
    hashes = Column(JSONB, nullable=True)          # {doc_type: sha256}
    master_hash = Column(String(64), nullable=True)
    anchoring = Column(JSONB, nullable=True)       # {doc_type|master: {tx_hash, ...}}
    download_urls = Column(JSONB, nullable=True)   # {doc_type: s3_url}
    error = Column(String(1000), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Index("ix_evidence_packs_user_status", EvidencePack.user_id, EvidencePack.status)
