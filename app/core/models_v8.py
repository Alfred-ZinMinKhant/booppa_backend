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
