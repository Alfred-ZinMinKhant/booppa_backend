"""
Booppa V11 — Enterprise Modules
================================
ComplianceRequirement  — Singapore regulation → required evidence type mapping
ManagedVendor          — Enterprise buyer's monitored vendor portfolio
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text, Boolean,
    JSON, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from app.core.db import Base


# ── ComplianceRequirement ──────────────────────────────────────────────────────
# Defines what Singapore regulations exist and which evidence types satisfy them.
# This table is seeded at startup — one row per regulation.
#
# regulation_key examples: PDPA | ACRA | GEBIZ | MAS
# required_evidence_types: list of framework strings that satisfy this regulation
#   e.g. ["pdpa_scan", "compliance_notarization", "pdpa_free_scan"]
#
# Evidence is pulled from:
#   reports(framework) — PDPA scans and notarization reports
#   proofs             — blockchain-anchored document proofs
#   evidence_packages  — curated document bundles
class ComplianceRequirement(Base):
    __tablename__ = "compliance_requirements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Short machine key — used by API to scope evidence queries
    regulation_key = Column(String(50), unique=True, nullable=False, index=True)

    # Display name shown in the frontend locker
    display_name = Column(String(255), nullable=False)

    # Brief description of what this regulation requires
    description = Column(Text, nullable=True)

    # Which report frameworks satisfy this regulation
    # e.g. ["pdpa_scan", "pdpa_full", "pdpa_free_scan", "compliance_notarization"]
    required_frameworks = Column(JSON, nullable=False, default=list)

    # Whether the regulation requires blockchain notarization to be considered "met"
    requires_notarization = Column(Boolean, default=False, nullable=False)

    # External reference URL (e.g. official PDPC page)
    reference_url = Column(String(500), nullable=True)

    # Display order on the locker page
    sort_order = Column(Integer, default=0, nullable=False)

    # Whether this regulation is active on the locker
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── ManagedVendor ──────────────────────────────────────────────────────────────
# An enterprise buyer's monitored vendor portfolio entry.
# One row per (enterprise_user_id, vendor_user_id) pair.
#
# status: ACTIVE | ARCHIVED | PENDING_INVITE
#
# ARCHITECTURAL NOTES:
#   - Vendors are NEVER notified that they are in a portfolio
#   - compliance_score_snapshot is cached from VendorStatusSnapshot at add/refresh time
#   - invite_token is set when buyer sends a compliance invite to a vendor (Phase 3)
class ManagedVendor(Base):
    __tablename__ = "managed_vendors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The enterprise buyer who added this vendor
    enterprise_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The vendor being monitored (NULL if not yet a registered user — e.g. pending invite)
    vendor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Vendor name/email for display when vendor_user_id is NULL
    vendor_name = Column(String(255), nullable=True)
    vendor_email = Column(String(255), nullable=True, index=True)

    # ACTIVE | ARCHIVED | PENDING_INVITE
    status = Column(String(30), nullable=False, default="ACTIVE", index=True)

    # Buyer-defined internal label (e.g. "Primary IT Supplier")
    label = Column(String(255), nullable=True)

    # Risk threshold: if riskSignal exceeds this, surface in alerts
    # CLEAN | WATCH | FLAGGED | CRITICAL
    alert_threshold = Column(String(20), nullable=False, default="WATCH")

    # Cached compliance snapshot fields (refreshed when viewed or on schedule)
    cached_risk_signal = Column(String(20), nullable=True)
    cached_verification_depth = Column(String(50), nullable=True)
    cached_procurement_readiness = Column(String(50), nullable=True)
    cached_total_score = Column(Integer, nullable=True)
    cache_refreshed_at = Column(DateTime, nullable=True)

    # Phase 3 — compliance invite
    invite_token = Column(String(100), nullable=True, unique=True, index=True)
    invite_sent_at = Column(DateTime, nullable=True)
    invite_accepted_at = Column(DateTime, nullable=True)

    # Compliance score as reported by vendor's evidence locker (Phase 3)
    vendor_compliance_score = Column(Float, nullable=True)
    vendor_compliance_updated_at = Column(DateTime, nullable=True)

    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_user_id", "vendor_user_id",
            name="uq_managed_vendor_enterprise_vendor",
        ),
        Index("ix_managed_vendors_enterprise_status", "enterprise_user_id", "status"),
        Index("ix_managed_vendors_enterprise_risk", "enterprise_user_id", "cached_risk_signal"),
    )
