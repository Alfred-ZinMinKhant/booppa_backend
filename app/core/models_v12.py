"""
Booppa V12 (continued) — API keys
=================================
Webhooks, SSO, and Organisation membership models already live in
`models_enterprise.py` (org-keyed). This file only adds ApiKey, which is
user-scoped — your API token authenticates as you regardless of organisation.

Multi-subsidiary uses `parent_user_id` on the users table — see the matching
Alembic migration.
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Index, Float, Boolean, Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.db import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(120), nullable=False)
    prefix = Column(String(16), nullable=False, index=True)
    hashed_key = Column(String(64), nullable=False, unique=True, index=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


Index("ix_api_keys_user_active", ApiKey.user_id, ApiKey.revoked_at)


class PendingRfpIntake(Base):
    """RFP brief still to be filled in by a bundle buyer.

    Bundle SKUs that include an RFP component (rfp_accelerator, enterprise_bid_kit,
    compliance_evidence_pack) defer RFP generation until the buyer submits the
    description and intake facts. One row per bundle purchase; status transitions
    pending → submitted when the user posts /rfp-intake/{id}/submit, which queues
    fulfill_rfp_task.
    """

    __tablename__ = "pending_rfp_intakes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(255), nullable=True, index=True)
    rfp_product_type = Column(String(64), nullable=False)  # rfp_express / rfp_complete
    bundle_source = Column(String(64), nullable=False)     # rfp_accelerator / enterprise_bid_kit / compliance_evidence_pack
    vendor_url = Column(String(500), nullable=True)
    company_name = Column(String(255), nullable=True)
    # Singapore UEN (Business Registration No.) — collected at intake and required
    # before generation; the field GeBIZ procurement officers check first.
    uen = Column(String(50), nullable=True)
    # status: pending → submitted (queued) ; needs_more_info when a generated kit
    # was blocked at the placeholder gate and the buyer must complete missing facts.
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)


Index("ix_pending_rfp_user_status", PendingRfpIntake.user_id, PendingRfpIntake.status)


class RopaActivities(Base):
    """One row per declared processing activity for a buyer's ROPA Lite.

    PDPC Level 2 evidence for compliance_evidence_pack. Mirrors
    PendingRfpIntake's draft → submitted lifecycle and user_id-based
    identification (no bundle FK — the bundle isn't a row). A buyer typically
    declares 3–8 activities (payroll, marketing, CCTV, …). Rows are created in
    'draft' as the multi-row form is filled, then flipped to 'submitted' as a
    batch on Generate, which queues the ROPA PDF into the next Cover Sheet cycle.

    The six ROPA_INTAKE_SCHEMA fields (ropa_generator.py) get one column each —
    not a JSON blob — so the data stays queryable/auditable per-field, which is
    the whole point of a record that must survive a PDPC records request.
    """

    __tablename__ = "ropa_activities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bundle_source = Column(String(64), nullable=False, default="compliance_evidence_pack")
    status = Column(String(20), nullable=False, default="draft", server_default="draft")

    processing_purpose = Column(String(200), nullable=False)
    data_categories = Column(String(500), nullable=False)
    data_subjects = Column(String(200), nullable=False)
    retention_period = Column(String(300), nullable=False)
    cross_border_transfer = Column(String(400), nullable=False)
    legal_basis = Column(String(100), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)


Index("ix_ropa_activities_user_status", RopaActivities.user_id, RopaActivities.status)


class PdpaSelfDeclaration(Base):
    """One row per declared processing activity for a PDPA Level-2 self-declaration.

    Elevates the automated PDPA Quick Scan (Level 1) to PDPC Level 2 by letting
    the organisation self-declare its processing activities and accountability
    measures. Mirrors RopaActivities' draft → submitted lifecycle and user_id
    identification; the submitted set is rendered to an anchored PDF Report with
    framework="pdpa_self_declaration".

    Fields are one column each (not JSON) so the declaration is queryable and
    auditable per-field for a PDPC records request.
    """

    __tablename__ = "pdpa_self_declarations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source = Column(String(64), nullable=False, default="pdpa_quick_scan")
    status = Column(String(20), nullable=False, default="draft", server_default="draft")

    processing_purpose = Column(String(200), nullable=False)
    lawful_basis = Column(String(100), nullable=False)
    data_categories = Column(String(500), nullable=False)
    data_subjects = Column(String(200), nullable=False)
    recipients = Column(String(400), nullable=False)
    retention_period = Column(String(300), nullable=False)
    safeguards = Column(String(500), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)


Index(
    "ix_pdpa_self_declarations_user_status",
    PdpaSelfDeclaration.user_id,
    PdpaSelfDeclaration.status,
)


class VendorEvaluationFramework(Base):
    """A buyer's vendor-scoring weight profile.

    Powers two marketed features at once:
      • Buyer Professional — "customisable risk-scoring weights per category"
        (the buyer edits the five component weights on a CUSTOM framework).
      • Buyer Enterprise — "custom evaluation frameworks (MAS TRM for fintechs,
        MOH for healthcare …)" (named, optionally sector-scoped profiles, seeded
        with built-in templates).

    A framework is just a named set of the five VendorScoreEngine component
    weights (+ optional sector + criteria metadata). Vendor ranking re-computes
    the total from the vendor's already-stored component scores at read time
    using the resolved framework's weights — no per-framework score caching.

    Resolution order for a given vendor (see scoring.resolve_weights):
      sector-matched org framework → org's active_framework_id → DEFAULT weights.

    Org-scoped so it's shared across a buyer team's seats. Built-in templates
    (is_builtin=True) are seeded per org; users create CUSTOM ones on top.
    """

    __tablename__ = "vendor_evaluation_frameworks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(120), nullable=False)
    # DEFAULT | CUSTOM | MAS_TRM | MOH | GEBIZ
    framework_type = Column(String(32), nullable=False, default="CUSTOM", server_default="CUSTOM")
    # When set, this framework auto-applies to vendors tagged with this sector
    # (VendorSector). Lets "MAS TRM for fintech, MOH for healthcare" work off the
    # existing sector tags with no per-vendor wiring.
    sector = Column(String(120), nullable=True, index=True)

    # The five VendorScoreEngine component weights (should sum ≈ 1.0).
    weight_compliance = Column(Float, nullable=False, default=0.30)
    weight_visibility = Column(Float, nullable=False, default=0.20)
    weight_engagement = Column(Float, nullable=False, default=0.20)
    weight_recency = Column(Float, nullable=False, default=0.15)
    weight_procurement_interest = Column(Float, nullable=False, default=0.15)

    # Optional non-weight metadata (e.g. required evidence notes). Not applied to
    # component scoring yet — see the PR's documented boundary.
    criteria = Column(JSONB, nullable=True)

    is_builtin = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        # One framework per (org, sector) so sector resolution is unambiguous;
        # sector NULL rows (general/custom) are not constrained.
        UniqueConstraint("organisation_id", "name", name="uq_eval_framework_org_name"),
        Index("ix_eval_framework_org_sector", "organisation_id", "sector"),
    )

    def weights(self) -> dict:
        """Return the weight profile in VendorScoreEngine's key format."""
        return {
            "COMPLIANCE": self.weight_compliance,
            "VISIBILITY": self.weight_visibility,
            "ENGAGEMENT": self.weight_engagement,
            "RECENCY": self.weight_recency,
            "PROCUREMENT_INTEREST": self.weight_procurement_interest,
        }


Index(
    "ix_eval_framework_org_active",
    VendorEvaluationFramework.organisation_id,
    VendorEvaluationFramework.framework_type,
)


class PdpaBulkScanBatch(Base):
    """One admin-uploaded CSV/XLSX of companies to run PDPA free scans against.

    Admin-only testing/prospecting tool: the operator uploads up to ~1,000
    (company_name, website_url) rows; each row becomes a PdpaBulkScanItem and a
    rate-limited Celery task on the `reports` queue. No User row, payment, PDF,
    AI, or blockchain involvement — items call run_free_scan() directly.
    """

    __tablename__ = "pdpa_bulk_scan_batches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Admin username from the admin JWT / basic auth — not a users.id FK, since
    # admin operators are not application users.
    created_by = Column(String(120), nullable=True)
    filename = Column(String(255), nullable=True)
    total = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PdpaBulkScanItem(Base):
    __tablename__ = "pdpa_bulk_scan_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("pdpa_bulk_scan_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_name = Column(String(255), nullable=False)
    website_url = Column(String(500), nullable=False)
    # pending → running → done | failed
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    # run_free_scan() response: score, risk_level, findings, …
    result = Column(JSONB, nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)


Index("ix_pdpa_bulk_items_batch_status", PdpaBulkScanItem.batch_id, PdpaBulkScanItem.status)


class BuyerSupplierAlert(Base):
    """Dedup ledger for event-triggered supplier drift alerts (#1).

    One row per (buyer, watched supplier). Records the last state we alerted the
    buyer about, so the drift sweep only emails when a *new* material change
    crosses a threshold — a score drop, a flip into FLAGGED/CRITICAL, or an
    approaching certificate expiry — instead of re-sending the same alert every
    run. `last_*` columns hold the state at the moment of the last alert; the
    sweep compares live status against them and updates on send.
    """

    __tablename__ = "buyer_supplier_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The watchlist vendor_ref (marketplace slug / free-form id) as stored on the
    # VendorWatchlistItem — not necessarily a resolvable users.id.
    vendor_ref = Column(String(255), nullable=False, index=True)
    last_trust_score = Column(Integer, nullable=True)
    last_risk_signal = Column(String(50), nullable=True)
    # Cert-expiry alerts: the expires_at we last warned about, so we don't renag.
    last_expiry_warned_for = Column(DateTime, nullable=True)
    last_reason = Column(String(64), nullable=True)
    last_alerted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


UniqueConstraint(
    BuyerSupplierAlert.buyer_user_id,
    BuyerSupplierAlert.vendor_ref,
    name="uq_buyer_supplier_alert",
)
Index(
    "ix_buyer_supplier_alert_buyer_ref",
    BuyerSupplierAlert.buyer_user_id,
    BuyerSupplierAlert.vendor_ref,
    unique=True,
)


class BuyerTenderPush(Base):
    """Dedup ledger for per-tender high-fit push alerts (#4).

    One row per (buyer, tender). Records that we already emailed this buyer an
    immediate "a strongly-matching tender just opened" push for this GeBIZ tender,
    so the ingest-triggered sweep never re-pushes the same tender — and the buyer
    still sees it in the monthly digest's roundup without a duplicate one-off.
    """

    __tablename__ = "buyer_tender_pushes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # GebizTender.tender_no — the stable public tender identifier.
    tender_no = Column(String(100), nullable=False, index=True)
    # Why it matched: the resolved tender sector at push time (audit / debugging).
    sector = Column(String(255), nullable=True)
    pushed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


UniqueConstraint(
    BuyerTenderPush.buyer_user_id,
    BuyerTenderPush.tender_no,
    name="uq_buyer_tender_push",
)
Index(
    "ix_buyer_tender_push_buyer_tender",
    BuyerTenderPush.buyer_user_id,
    BuyerTenderPush.tender_no,
    unique=True,
)
