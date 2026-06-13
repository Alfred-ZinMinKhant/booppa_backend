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
    Column, String, DateTime, ForeignKey, Index, Float, Boolean, UniqueConstraint,
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
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)


Index("ix_pending_rfp_user_status", PendingRfpIntake.user_id, PendingRfpIntake.status)


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
