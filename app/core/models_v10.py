"""
Booppa V10 — New Models
=======================
MarketplaceVendor      — CSV-imported vendor directory for marketplace
FunnelEvent            — visitor → stage conversion tracking
RevenueEvent           — MRR tracking, churn triggers
SubscriptionSnapshot   — monthly cohort aggregates
QuarterlyLeaderboard   — immutable quarterly ranking snapshots with trophy badges
Achievement            — vendor achievements per quarter
ScoreMilestone         — achievement tracking (TOP_10_PCT, TIER_UP, SCORE_500, etc.)
PrestigeSlot           — sector/tier limited availability slots
Referral               — P9 referral program
EnterpriseInviteToken  — P2 invite links
ApiUsage               — usage metering per plan
CertificateLog         — PDF certificate generation audit
DiscoveredVendor       — vendors found via GeBIZ / ACRA import
ImportBatch            — CSV import batch tracking
FeatureFlag            — Redis-backed feature flags persisted to DB
TenderShortlist        — GeBIZ tender catalogue for win-probability tool
"""

import uuid
import enum
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text, Boolean,
    JSON, ForeignKey, UniqueConstraint, Index, BigInteger,
)
from sqlalchemy.dialects.postgresql import UUID
from app.core.db import Base


# ── Enums ──────────────────────────────────────────────────────────────────────

class VendorTierEnum(str, enum.Enum):
    STANDARD = "STANDARD"
    STRATEGIC = "STRATEGIC"
    ELITE = "ELITE"


class FunnelStage(str, enum.Enum):
    VISIT = "VISIT"
    SIGNUP = "SIGNUP"
    TRIAL = "TRIAL"
    SCAN = "SCAN"
    CHECKOUT = "CHECKOUT"
    PAYMENT = "PAYMENT"
    VERIFICATION = "VERIFICATION"
    ACTIVE = "ACTIVE"


class RevenueType(str, enum.Enum):
    NEW_MRR = "NEW_MRR"
    EXPANSION = "EXPANSION"
    CONTRACTION = "CONTRACTION"
    CHURN = "CHURN"
    REACTIVATION = "REACTIVATION"
    ONE_TIME = "ONE_TIME"


class MilestoneType(str, enum.Enum):
    TOP_10_PCT = "TOP_10_PCT"
    TOP_5_PCT = "TOP_5_PCT"
    TOP_1_PCT = "TOP_1_PCT"
    TIER_UP = "TIER_UP"
    SCORE_500 = "SCORE_500"
    SCORE_750 = "SCORE_750"
    FIRST_NOTARIZATION = "FIRST_NOTARIZATION"
    FIRST_RFP = "FIRST_RFP"
    ENTERPRISE_READY = "ENTERPRISE_READY"


# ── MarketplaceVendor ──────────────────────────────────────────────────────────
# CSV-imported vendor directory for marketplace bootstrap.
# These are NOT registered users — they are discovered companies.
# When a company registers, link via UEN or domain to upgrade to full User.
class MarketplaceVendor(Base):
    __tablename__ = "marketplace_vendors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    company_name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    domain = Column(String(255), nullable=True, index=True)
    website = Column(String(500), nullable=True)
    uen = Column(String(50), nullable=True, unique=True, index=True)

    industry = Column(String(100), nullable=True, index=True)
    country = Column(String(100), default="Singapore", nullable=False, index=True)
    city = Column(String(100), nullable=True)
    short_description = Column(Text, nullable=True)

    linkedin_url = Column(String(500), nullable=True)
    crunchbase_url = Column(String(500), nullable=True)

    # Scan status: NONE | QUEUED | SCANNING | COMPLETE | FAILED
    scan_status = Column(String(20), default="NONE", nullable=False, index=True)
    scan_completed_at = Column(DateTime, nullable=True)

    # Link to registered user (set when company claims profile)
    claimed_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    claimed_at = Column(DateTime, nullable=True)

    # Import tracking
    import_batch_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    source = Column(String(50), default="csv", nullable=False)  # csv | acra | gebiz | manual

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_marketplace_vendors_industry_country", "industry", "country"),
    )


# ── DiscoveredVendor ───────────────────────────────────────────────────────────
# Vendors discovered from GeBIZ tenders or ACRA registry.
# Source-of-truth for external vendor intelligence before they register.
class DiscoveredVendor(Base):
    __tablename__ = "discovered_vendors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    company_name = Column(String(255), nullable=False, index=True)
    uen = Column(String(50), nullable=True, unique=True, index=True)
    domain = Column(String(255), nullable=True, index=True)
    entity_type = Column(String(100), nullable=True)  # PRIVATE COMPANY, SOLE-PROP, etc.
    registration_date = Column(String(50), nullable=True)

    industry = Column(String(100), nullable=True, index=True)
    country = Column(String(100), default="Singapore", nullable=False)
    city = Column(String(100), nullable=True)

    # GeBIZ data
    gebiz_supplier = Column(Boolean, default=False)
    gebiz_contracts_count = Column(Integer, default=0)
    gebiz_total_value = Column(Float, default=0.0)

    source = Column(String(50), nullable=False)  # acra | gebiz | manual
    source_data = Column(JSON, nullable=True)  # Raw data from source

    claimed_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── ImportBatch ────────────────────────────────────────────────────────────────
# Tracks CSV import batches for audit and deduplication.
class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String(255), nullable=False)
    source = Column(String(50), nullable=False)  # csv | acra | gebiz
    total_rows = Column(Integer, default=0)
    imported_count = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    errors = Column(JSON, default=list)

    # PENDING | PROCESSING | COMPLETE | FAILED
    status = Column(String(20), default="PENDING", nullable=False, index=True)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── FunnelEvent ────────────────────────────────────────────────────────────────
# Tracks visitor progression through the conversion funnel.
class FunnelEvent(Base):
    __tablename__ = "funnel_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id = Column(String(100), nullable=True, index=True)

    stage = Column(String(50), nullable=False, index=True)  # FunnelStage values
    previous_stage = Column(String(50), nullable=True)

    # Context
    source = Column(String(100), nullable=True)  # organic | referral | direct | ad
    utm_source = Column(String(100), nullable=True)
    utm_medium = Column(String(100), nullable=True)
    utm_campaign = Column(String(100), nullable=True)

    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    metadata_json = Column("metadata", JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_funnel_events_stage_created", "stage", "created_at"),
    )


# ── RevenueEvent ──────────────────────────────────────────────────────────────
# Tracks revenue events for MRR calculation, churn detection, cohort analysis.
class RevenueEvent(Base):
    __tablename__ = "revenue_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    event_type = Column(String(50), nullable=False, index=True)  # RevenueType values
    amount_cents = Column(Integer, nullable=False)  # Amount in cents (SGD)
    currency = Column(String(3), default="SGD", nullable=False)

    product_slug = Column(String(100), nullable=True, index=True)
    stripe_invoice_id = Column(String(255), nullable=True, unique=True)
    stripe_subscription_id = Column(String(255), nullable=True)

    # Period for subscription events
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)

    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_revenue_events_type_created", "event_type", "created_at"),
    )


# ── SubscriptionSnapshot ──────────────────────────────────────────────────────
# Monthly aggregate snapshot for cohort and MRR tracking.
class SubscriptionSnapshot(Base):
    __tablename__ = "subscription_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    month = Column(String(7), nullable=False, index=True)  # "2026-03"

    total_mrr_cents = Column(Integer, default=0)
    new_mrr_cents = Column(Integer, default=0)
    expansion_cents = Column(Integer, default=0)
    contraction_cents = Column(Integer, default=0)
    churn_cents = Column(Integer, default=0)

    active_subscriptions = Column(Integer, default=0)
    new_subscriptions = Column(Integer, default=0)
    churned_subscriptions = Column(Integer, default=0)

    computed_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("month", name="uq_subscription_snapshot_month"),
    )


# ── TenderShortlist ────────────────────────────────────────────────────────────
# GeBIZ tender catalogue used by the Tender Win Probability tool.
# Populated via admin import from GeBIZ open data or manual entry.
# base_rate is the sector/agency-calibrated baseline win rate (0.0–1.0).
class TenderShortlist(Base):
    __tablename__ = "tender_shortlists"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tender_no = Column(String(100), unique=True, nullable=False, index=True)
    sector = Column(String(100), nullable=False, index=True)
    agency = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)

    # Baseline win rate calibrated per sector/agency (0.0–1.0)
    base_rate = Column(Float, nullable=False, default=0.20)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_tender_shortlists_sector_agency", "sector", "agency"),
    )


# ── QuarterlyLeaderboard ──────────────────────────────────────────────────────
# Immutable quarterly ranking snapshot with trophy badges.
class QuarterlyLeaderboard(Base):
    __tablename__ = "quarterly_leaderboards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    quarter = Column(String(20), nullable=False, index=True)  # "Q1 2026"
    sector = Column(String(100), nullable=False, index=True)

    rank = Column(Integer, nullable=False)
    final_score = Column(Integer, nullable=False)
    percentile = Column(Float, nullable=False)

    tier = Column(String(20), nullable=False, index=True)  # ELITE | STRATEGIC | STANDARD
    is_top_vendor = Column(Boolean, default=False, index=True)  # Top 5 per sector

    # Trophy badge type: GOLD | SILVER | BRONZE | NONE
    trophy = Column(String(20), default="NONE", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("vendor_id", "quarter", "sector", name="uq_leaderboard_vendor_quarter_sector"),
        Index("ix_quarterly_leaderboard_quarter_sector_rank", "quarter", "sector", "rank"),
    )


# ── Achievement ────────────────────────────────────────────────────────────────
# Vendor achievements earned per quarter (e.g. "Top 10% in Cybersecurity Q1 2026").
class Achievement(Base):
    __tablename__ = "achievements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    achievement_type = Column(String(50), nullable=False, index=True)  # MilestoneType values
    label = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    quarter = Column(String(20), nullable=True, index=True)
    sector = Column(String(100), nullable=True)

    expires_at = Column(DateTime, nullable=True, index=True)
    awarded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_achievements_vendor_type", "vendor_id", "achievement_type"),
    )


# ── ScoreMilestone ─────────────────────────────────────────────────────────────
# Tracks when a vendor crosses a score/ranking threshold.
class ScoreMilestone(Base):
    __tablename__ = "score_milestones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    milestone_type = Column(String(50), nullable=False, index=True)
    value = Column(Float, nullable=True)  # Score value at milestone
    sector = Column(String(100), nullable=True)
    quarter = Column(String(20), nullable=True)

    reached_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── PrestigeSlot ───────────────────────────────────────────────────────────────
# Limited-availability top vendor slots per sector per tier.
# Top 5 ELITE vendors per sector get a prestige slot automatically.
class PrestigeSlot(Base):
    __tablename__ = "prestige_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    sector = Column(String(100), nullable=False, index=True)
    tier = Column(String(20), nullable=False, index=True)  # ELITE | STRATEGIC
    slot_number = Column(Integer, nullable=False)  # 1-5

    # Active period
    activated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)

    quarter = Column(String(20), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("sector", "tier", "slot_number", "quarter", name="uq_prestige_slot"),
        Index("ix_prestige_slots_sector_active", "sector", "is_active"),
    )


# ── Referral ───────────────────────────────────────────────────────────────────
# P9 referral program tracking.
class Referral(Base):
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referrer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    referred_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    referral_code = Column(String(50), unique=True, nullable=False, index=True)
    referred_email = Column(String(255), nullable=True)

    # PENDING | SIGNED_UP | CONVERTED | EXPIRED
    status = Column(String(20), default="PENDING", nullable=False, index=True)

    # Reward tracking
    reward_type = Column(String(50), nullable=True)  # CREDIT | DISCOUNT | FEATURE_UNLOCK
    reward_amount_cents = Column(Integer, default=0)
    reward_claimed = Column(Boolean, default=False)
    reward_claimed_at = Column(DateTime, nullable=True)

    converted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ── EnterpriseInviteToken ─────────────────────────────────────────────────────
# P2 invite links for enterprise onboarding.
class EnterpriseInviteToken(Base):
    __tablename__ = "enterprise_invite_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enterprise_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    token = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=True)
    role = Column(String(50), default="MEMBER", nullable=False)  # ADMIN | MEMBER | VIEWER

    # PENDING | ACCEPTED | EXPIRED | REVOKED
    status = Column(String(20), default="PENDING", nullable=False, index=True)

    accepted_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── ApiUsage ───────────────────────────────────────────────────────────────────
# Usage metering per plan for rate limiting and billing.
class ApiUsage(Base):
    __tablename__ = "api_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    endpoint = Column(String(255), nullable=False, index=True)
    method = Column(String(10), nullable=False)
    month = Column(String(7), nullable=False, index=True)  # "2026-03"

    request_count = Column(Integer, default=0)
    last_request_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "endpoint", "method", "month", name="uq_api_usage_user_endpoint_month"),
    )


# ── CertificateLog ────────────────────────────────────────────────────────────
# PDF certificate generation audit trail.
class CertificateLog(Base):
    __tablename__ = "certificate_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    certificate_type = Column(String(50), nullable=False)  # VERIFICATION | NOTARIZATION | RFP | PDPA
    report_id = Column(UUID(as_uuid=True), nullable=True)
    evidence_package_id = Column(UUID(as_uuid=True), nullable=True)

    file_key = Column(String(500), nullable=True)  # S3 key
    file_hash = Column(String(64), nullable=True)  # SHA-256 of PDF

    downloaded_at = Column(DateTime, nullable=True)
    download_count = Column(Integer, default=0)
    download_ip = Column(String(45), nullable=True)

    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── FeatureFlag ────────────────────────────────────────────────────────────────
# Persisted feature flag state (source of truth in Redis, DB is backup).
class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flag_name = Column(String(100), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=False, nullable=False)
    description = Column(Text, nullable=True)

    # Phase this flag belongs to (1-4)
    phase = Column(Integer, default=1, nullable=False)

    # Auto-activation thresholds (JSON: { "min_vendors": 500, "min_rfps": 100 })
    activation_thresholds = Column(JSON, nullable=True)

    # Who/what enabled it
    enabled_by = Column(String(100), nullable=True)  # "auto" | admin user ID
    enabled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
