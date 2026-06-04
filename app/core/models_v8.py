"""
Booppa V8 — New Models
======================
VendorStatusSnapshot  — trust-engine write-through cache
ScoreSnapshot         — per-snapshot score history with breakdown
NotarizationMetadata  — Dual Silent Standard elevation layer (1:1 with elevated vendors)
RfpRequirement        — enterprise procurement requirement spec
RfpRequirementFlag    — point-in-time evaluation result (MEETS/PARTIAL/MISSING)
AnomalyEvent          — structured risk signal (replaces governance-record proxy)
EvidencePackage       — notarized document bundle (feeds elevation depth calculation)

Also adds is_primary column to VendorSector (see migration).
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text, Boolean,
    JSON, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.db import Base


# ── VendorStatusSnapshot ───────────────────────────────────────────────────────
# Write-through cache for VendorStatusEngine outputs.
# Re-computed on every ScoreSnapshot write and on demand.
# Vendors never see this table directly; it powers the procurement layer.
class VendorStatusSnapshot(Base):
    __tablename__ = "vendor_status_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Status levels — all derived from trust facts, never from payment/plan
    # UNVERIFIED | BASIC | STANDARD | DEEP | CERTIFIED
    verification_depth = Column(String(50), default="UNVERIFIED", index=True, nullable=False)
    # ACTIVE | STALE | INACTIVE | NONE
    monitoring_activity = Column(String(50), default="NONE", index=True, nullable=False)
    # CLEAN | WATCH | FLAGGED | CRITICAL
    risk_signal = Column(String(50), default="CLEAN", index=True, nullable=False)
    # READY | CONDITIONAL | NEEDS_ATTENTION | NOT_READY
    procurement_readiness = Column(String(50), default="NOT_READY", index=True, nullable=False)

    # Risk-adjusted percentile (from SectorPercentileEngine)
    # Does NOT affect vendor_scores.total_score
    risk_adjusted_pct = Column(Float, default=50.0, nullable=False)

    # Dual Silent Standard fields (cached from NotarizationMetadata at compute time)
    # SILENT_RISK_CAPTURE | ELEVATED_VERIFIED
    dual_silent_mode = Column(String(50), default="SILENT_RISK_CAPTURE", nullable=False)
    notarization_depth = Column(Integer, default=0, nullable=False)   # 0–5 scale
    evidence_count = Column(Integer, default=0, nullable=False)        # COMPLETE notarizations + READY evidence
    confidence_score = Column(Float, default=0.0, nullable=False)      # 0–100 composite

    # Logic version — stale rows (version != current) should be recomputed
    version = Column(String(20), default="v2", nullable=False)
    computed_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── ScoreSnapshot ──────────────────────────────────────────────────────────────
# Immutable historical record for each score computation.
# Complements VendorScore (which is the CURRENT score).
# Used for: trajectory, volatility, score history, procurement timeline.
class ScoreSnapshot(Base):
    __tablename__ = "score_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    base_score = Column(Float, nullable=False)
    multiplier = Column(Float, nullable=False, default=1.0)
    final_score = Column(Integer, nullable=False)

    # JSON breakdown: { compliance, visibility, engagement, recency, procurement_interest }
    breakdown = Column(JSON, nullable=True)

    # Sector percentile at snapshot time (50 = median)
    sector_percentile = Column(Float, default=50.0, nullable=False)

    # Audit fields
    score_version = Column(String(20), nullable=True)   # formula version e.g. "1.0"
    score_hash = Column(String(64), nullable=True, index=True)  # SHA-256 of key fields

    quarter = Column(String(20), nullable=True, index=True)  # e.g. "Q1 2026"
    snapshot_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_score_snapshots_vendor_snapshot", "vendor_id", "snapshot_at"),
        Index("ix_score_snapshots_final_score", "final_score"),
    )


# ── NotarizationMetadata ───────────────────────────────────────────────────────
# Dual Silent Standard — Elevation Layer.
# One row per ELEVATED vendor only. STANDARD vendors have no row here.
#
# ARCHITECTURAL GUARANTEES:
#   - Does NOT affect VendorScore or ScoreSnapshot
#   - Does NOT affect procurement ordering (ordering reads score only)
#   - Does NOT trigger monetization or plan gates
#   - Elevation is earned through evidence depth, not payment
#
# structuralLevel is always ELEVATED when a row exists.
class NotarizationMetadata(Base):
    __tablename__ = "notarization_metadata"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    notarized_at = Column(DateTime, nullable=False)   # timestamp of qualifying notarization

    # Deterministic opaque identifier: SHA-256(vendorId:notarizationHash).slice(0,32)
    # Stable for the lifecycle of a notarization — does not change as depth grows
    validation_id = Column(String(64), unique=True, nullable=False)

    # BASIC | ENHANCED | DEEP | ENTERPRISE
    verification_depth = Column(String(50), nullable=False, index=True)

    # Always ELEVATED when row exists; STANDARD = no row
    structural_level = Column(String(50), nullable=False, default="ELEVATED", index=True)

    # SHA-256(vendorId + notarized_at ISO + validation_id) — independently verifiable
    public_hash = Column(String(64), nullable=False)

    logic_version = Column(String(20), nullable=False)

    # Drift signal fields — used by procurement ordering (sector-scoped)
    evidence_count = Column(Integer, default=0, nullable=False)          # GLOBAL count
    evidence_count_by_sector = Column(JSON, default=dict, nullable=False)  # { sector: count }
    confidence_score = Column(Float, default=0.0, nullable=False)         # 0–100 composite

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_notarization_metadata_structural_confidence", "structural_level", "confidence_score"),
        Index("ix_notarization_metadata_structural_evidence", "structural_level", "evidence_count"),
        Index("ix_notarization_metadata_notarized_at", "notarized_at"),
    )


# ── RfpRequirement ─────────────────────────────────────────────────────────────
# Enterprise-defined requirement specification for a procurement search.
# Vendors are NEVER shown or notified about requirements defined against them.
# Evaluation flags are purely informational — no auto-block mechanism.
class RfpRequirement(Base):
    __tablename__ = "rfp_requirements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    label = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Requirement parameters — all have safe defaults (most lenient possible)
    # NONE | UNVERIFIED | BASIC | STANDARD | DEEP | CERTIFIED
    minimum_verification_depth = Column(String(50), default="NONE", nullable=False)
    minimum_percentile = Column(Float, default=0.0, nullable=False)   # 0 = no requirement
    require_active_monitoring = Column(Boolean, default=False, nullable=False)
    require_no_open_anomalies = Column(Boolean, default=False, nullable=False)
    minimum_days_until_expiry = Column(Integer, default=0, nullable=False)

    # Lifecycle
    archived = Column(Boolean, default=False, nullable=False, index=True)
    archived_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── RfpRequirementFlag ─────────────────────────────────────────────────────────
# Point-in-time evaluation of a vendor against an RfpRequirement.
# MEETS / PARTIAL / MISSING are informational — no blocking behaviour.
# Vendors are NEVER shown these flags.
# Re-evaluation replaces the previous result (upsert on vendor_id + requirement_id).
class RfpRequirementFlag(Base):
    __tablename__ = "rfp_requirement_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requirement_id = Column(
        UUID(as_uuid=True),
        ForeignKey("rfp_requirements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # MEETS | PARTIAL | MISSING
    overall_status = Column(String(20), nullable=False, index=True)

    # Array of { requirement_key, status, actual, required, detail }
    flag_details = Column(JSON, default=list, nullable=False)

    evaluated_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("vendor_id", "requirement_id", name="uq_rfp_flag_vendor_requirement"),
        Index("ix_rfp_requirement_flags_evaluated_at", "evaluated_at"),
    )


# ── AnomalyEvent ───────────────────────────────────────────────────────────────
# Structured risk signal for a vendor.
# Replaces the GovernanceRecord proxy used by compute_risk_signal().
#
# severity: LOW | MEDIUM | HIGH | CRITICAL
# status:   OPEN | RESOLVED | DISMISSED
# anomaly_type: free string (e.g. DOCUMENT_MISMATCH, EXPIRY_BREACH, DATA_INCONSISTENCY)
#
# ARCHITECTURAL NOTES:
#   - Only OPEN anomalies contribute to risk_signal in VendorStatusEngine
#   - Vendors are NEVER shown these records
#   - Resolving does not auto-clear the VendorStatusSnapshot — call upsert_status_snapshot()
class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # OPEN | RESOLVED | DISMISSED
    status = Column(String(20), nullable=False, default="OPEN", index=True)

    # LOW | MEDIUM | HIGH | CRITICAL
    severity = Column(String(20), nullable=False, default="LOW", index=True)

    # e.g. DOCUMENT_MISMATCH, EXPIRY_BREACH, DATA_INCONSISTENCY, SCORE_ANOMALY
    anomaly_type = Column(String(100), nullable=False, index=True)

    # Human-readable description (internal use only)
    description = Column(Text, nullable=True)

    # Arbitrary structured payload (source document IDs, diff values, etc.)
    metadata_json = Column("metadata", JSON, nullable=True)

    # Correlation chain (ties to GovernanceRecord that triggered this)
    correlation_id = Column(String(255), nullable=True, index=True)

    # Resolution tracking
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(255), nullable=True)   # admin user_id or system

    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_anomaly_events_vendor_status", "vendor_id", "status"),
        Index("ix_anomaly_events_vendor_severity", "vendor_id", "severity"),
        Index("ix_anomaly_events_status_severity", "status", "severity"),
    )


# ── EvidencePackage ────────────────────────────────────────────────────────────
# A curated, ready-to-certify bundle of documents for a vendor.
# Feeds into notarization_elevation.py elevation depth calculations
# (READY packages count toward DEEP / ENTERPRISE thresholds).
#
# status: DRAFT | UNDER_REVIEW | READY | REJECTED
# sector: the procurement sector this package is curated for
class EvidencePackage(Base):
    __tablename__ = "evidence_packages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # DRAFT | UNDER_REVIEW | READY | REJECTED
    status = Column(String(20), nullable=False, default="DRAFT", index=True)

    # STANDARD | EXPRESS | COMPLETE (v12 bug fix — was missing)
    tier = Column(String(20), nullable=True, index=True)

    # The sector this evidence bundle is curated for (matches VendorSector.sector)
    sector = Column(String(100), nullable=True, index=True)

    # Display title and internal description
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Proof IDs included in this bundle (references proofs.id array)
    proof_ids = Column(JSON, nullable=False, default=list)

    # Document count in bundle (denormalised for fast elevation queries)
    document_count = Column(Integer, nullable=False, default=0)

    # Reviewer notes (internal only)
    reviewer_notes = Column(Text, nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)

    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_evidence_packages_vendor_status", "vendor_id", "status"),
        Index("ix_evidence_packages_vendor_sector", "vendor_id", "sector"),
    )


# ── NotarizationCredit ────────────────────────────────────────────────────────
# Monthly notarization credit tracking for enterprise subscribers.
# One row per user per calendar month. Resets automatically each month.
#
# Hard monthly caps per plan. No "unlimited" tier — every plan has a ceiling
# so margins stay predictable and notarization can't be abused at scale.
#
# Standard Suite     : 50 / month
# Pro Suite          : 100 / month
# Enterprise Pro     : 200 / month
# Buyer Essentials   : 1 / month
# Buyer Professional : 5 / month
# Buyer Enterprise   : 20 / month
#
# Credits are consumed when a subscriber notarizes via the
# /notarize/upload endpoint without going through Stripe checkout.
ENTERPRISE_NOTARIZATION_LIMITS = {
    "enterprise":              200,
    "enterprise_monthly":      200,
    "enterprise_pro":          200,
    "enterprise_pro_monthly":  200,
    "standard_suite":          50,
    "standard_suite_monthly":  50,
    "pro_suite":               100,
    "pro_suite_monthly":       100,
    # Vendor Pro: 1 notarization included per month (the tier's marquee feature).
    "vendor_pro":              1,
    "vendor_pro_monthly":      1,
    "vendor_pro_annual":       1,
    # Buyer ladder — notarizations bundled into each tier.
    "buyer_starter":             1,
    "buyer_starter_monthly":     1,
    "buyer_starter_annual":      1,
    "buyer_pro":                 5,
    "buyer_pro_monthly":         5,
    "buyer_pro_annual":          5,
    "buyer_enterprise":          20,
    "buyer_enterprise_monthly":  20,
    "buyer_enterprise_annual":   20,
    # Batch notarization subscriptions — monthly allowance, resets each cycle.
    "compliance_notarization_10": 10,
    "compliance_notarization_50": 50,
}


# ── VendorScanLedger ──────────────────────────────────────────────────────────
# Buyer-ladder scan quotas. One row per (buyer, vendor, month, scan_type).
# Unique constraint means re-scanning the same vendor within the same month
# for the same scan tier is a no-op (insert-or-ignore semantics in the helper).
# Count rows by (buyer_id, month, scan_type) to compute usage; subtract from
# BUYER_SCAN_LIMITS[plan][scan_type] for remaining.
#
# Scan types (marketing language → enum):
#   QUICK    — L1 ACRA + MAS watchlist + PDPA flag
#   DEEP     — L2 8-dimension PDPA + certifications + financial risk
#   EVIDENCE — L3 blockchain evidence retrieval + complete dossier
class VendorScanLedger(Base):
    __tablename__ = "vendor_scan_ledger"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The vendor being scanned — can reference users.id (claimed) or
    # marketplace_vendors.id (unclaimed). No FK so either source works.
    vendor_id = Column(UUID(as_uuid=True), nullable=False)
    month = Column(String(7), nullable=False)              # "YYYY-MM"
    scan_type = Column(String(20), nullable=False)         # "QUICK" | "DEEP" | "EVIDENCE"
    plan_at_consumption = Column(String(64))               # audit trail
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "buyer_id", "vendor_id", "month", "scan_type",
            name="uq_scan_ledger_buyer_vendor_month_type",
        ),
        Index(
            "ix_scan_ledger_buyer_month_type",
            "buyer_id", "month", "scan_type",
        ),
    )


class NotarizationCredit(Base):
    __tablename__ = "notarization_credits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # e.g. "2026-04" — one row per month
    month = Column(String(7), nullable=False, index=True)

    # How many notarizations used this month
    used = Column(Integer, nullable=False, default=0)

    # Monthly cap at time of row creation (snapshot for audit trail)
    monthly_limit = Column(Integer, nullable=False, default=5000)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_notarization_credit_user_month"),
    )


# ── ComplianceDriftEvent ──────────────────────────────────────────────────────
# PDPA Monitor drift detection: a row is created when a quarterly re-scan
# shows a material drop in compliance posture compared to the previous scan.
class ComplianceDriftEvent(Base):
    __tablename__ = "compliance_drift_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    framework = Column(String(64), nullable=False, default="pdpa_quick_scan")
    previous_report_id = Column(UUID(as_uuid=True), nullable=True)
    current_report_id = Column(UUID(as_uuid=True), nullable=True)
    previous_score = Column(Float, nullable=True)
    current_score = Column(Float, nullable=True)
    delta = Column(Float, nullable=True)             # current - previous
    delta_pct = Column(Float, nullable=True)         # signed % change relative to previous
    severity = Column(String(16), nullable=False, default="WARNING")  # INFO | WARNING | CRITICAL
    details = Column(JSON, nullable=True)            # per-dimension before/after if available
    notified = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_compliance_drift_vendor_created", "vendor_id", "created_at"),
    )


# ── PdpaDimensionHistory ──────────────────────────────────────────────────────
# One row per PDPA-dimension per completed scan. Lets the monthly drift task
# detect dimension-level Compliant → Non-Compliant flips that overall risk
# scoring would otherwise hide. Populated from process_report_workflow.
class PdpaDimensionHistory(Base):
    __tablename__ = "pdpa_dimension_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_id = Column(UUID(as_uuid=True), nullable=True)
    framework = Column(String(64), nullable=False, default="pdpa_quick_scan")
    dimension_name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)   # Compliant | Partial | Non-Compliant
    score = Column(Integer, nullable=False)
    captured_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_pdpa_dim_history_vendor_dim_time", "vendor_id", "dimension_name", "captured_at"),
    )


# ── FindingRemediation ────────────────────────────────────────────────────────
# A user can mark a specific finding as "fixed" (or won't-fix) from their
# report view. On the next completed scan for the same vendor + framework,
# the worker auto-confirms whether the finding still appears (regressed) or
# is gone (confirmed). See app/services/finding_keys.py for how `finding_key`
# is derived deterministically from scan data.
class FindingRemediation(Base):
    __tablename__ = "finding_remediations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_id = Column(UUID(as_uuid=True), nullable=True)
    finding_key = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default="fixed")
    # ^ open | fixed | wontfix
    confirmation_status = Column(String(32), nullable=False, default="pending")
    # ^ pending | confirmed | regressed | stale
    marked_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    marked_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    confirming_report_id = Column(UUID(as_uuid=True), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_finding_remediations_vendor_key_time", "vendor_id", "finding_key", "marked_at"),
        Index("ix_finding_remediations_vendor_status", "vendor_id", "confirmation_status"),
    )
