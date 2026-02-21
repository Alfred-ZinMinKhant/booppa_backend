import uuid
import enum
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, JSON, Boolean, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.db import Base

class VerificationLevel(enum.Enum):
    BASIC = "BASIC"
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"
    GOVERNMENT = "GOVERNMENT"

class LifecycleStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    UNDER_REVIEW = "UNDER_REVIEW"
    SUSPENDED = "SUSPENDED"

class OrganizationType(enum.Enum):
    GOVERNMENT = "GOVERNMENT"
    GLC = "GLC"
    CORPORATE = "CORPORATE"
    SME = "SME"
    UNKNOWN = "UNKNOWN"

class LeadPriority(enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class LeadStatus(enum.Enum):
    NEW = "NEW"
    CONTACTED = "CONTACTED"
    QUALIFIED = "QUALIFIED"
    MEETING_SCHEDULED = "MEETING_SCHEDULED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    NEGOTIATION = "NEGOTIATION"
    WON = "WON"
    LOST = "LOST"

class MeetingStatus(enum.Enum):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    NO_SHOW = "NO_SHOW"

class VendorScore(Base):
    __tablename__ = "vendor_scores"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    compliance_score = Column(Integer, default=0)
    visibility_score = Column(Integer, default=0)
    engagement_score = Column(Integer, default=0)
    recency_score = Column(Integer, default=0)
    procurement_interest_score = Column(Integer, default=0)
    total_score = Column(Integer, default=0, index=True)
    last_calculation = Column(DateTime, default=datetime.utcnow)
    calculation_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VerifyRecord(Base):
    __tablename__ = "verify_records"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    compliance_score = Column(Integer, default=0)
    visibility_score = Column(Integer, default=0)
    verification_level = Column(SQLEnum(VerificationLevel), default=VerificationLevel.BASIC)
    last_refreshed_at = Column(DateTime, default=datetime.utcnow)
    lifecycle_status = Column(SQLEnum(LifecycleStatus), default=LifecycleStatus.ACTIVE, index=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    correlation_id = Column(String(255), nullable=True)

class Proof(Base):
    __tablename__ = "proofs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    verify_id = Column(UUID(as_uuid=True), ForeignKey("verify_records.id", ondelete="CASCADE"), index=True)
    hash_value = Column("hash", String(255), unique=True, index=True)
    title = Column(String(255))
    compliance_score = Column(Integer, nullable=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    correlation_id = Column(String(255), nullable=True)

class ProofView(Base):
    __tablename__ = "proof_views"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    verify_id = Column(UUID(as_uuid=True), ForeignKey("verify_records.id", ondelete="CASCADE"), index=True)
    proof_id = Column(UUID(as_uuid=True), ForeignKey("proofs.id", ondelete="CASCADE"))
    ip = Column(String(45))
    user_agent = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    domain = Column(String(255), nullable=True, index=True)
    asn = Column(String(255), nullable=True)
    org = Column(String(255), nullable=True)
    country = Column(String(50), nullable=True)
    city = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True)
    correlation_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class EnterpriseProfile(Base):
    __tablename__ = "enterprise_profiles"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), unique=True)
    organization_type = Column(SQLEnum(OrganizationType), default=OrganizationType.UNKNOWN, index=True)
    industry_inference = Column(String(255), nullable=True)
    visit_frequency = Column(Integer, default=0)
    unique_vendors_viewed = Column(Integer, default=0)
    total_views = Column(Integer, default=0)
    last_activity = Column(DateTime, default=datetime.utcnow)
    behavioral_score = Column(Integer, default=0, index=True)
    procurement_intent_score = Column(Integer, default=0)
    is_government = Column(Boolean, default=False)
    active_procurement = Column(Boolean, default=False, index=True)
    procurement_window_start = Column(DateTime, nullable=True)
    procurement_window_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class DomainActivity(Base):
    __tablename__ = "domain_activities"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), unique=True, index=True)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("enterprise_profiles.id"), nullable=True)
    total_views = Column(Integer, default=0)
    unique_proofs = Column(Integer, default=0)
    unique_vendors = Column(Integer, default=0)
    last_seen = Column(DateTime, default=datetime.utcnow, index=True)
    is_government = Column(Boolean, default=False, index=True)
    enterprise_triggered = Column(Boolean, default=False)
    triggered_at = Column(DateTime, nullable=True)
    trigger_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class GovernanceRecord(Base):
    __tablename__ = "governance_records"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(String(100))
    entity_type = Column(String(100))
    entity_id = Column(String(255))
    correlation_id = Column(String(255), index=True)
    user_id = Column(String(255), nullable=True)
    verify_id = Column(String(255), nullable=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class EcosystemIndex(Base):
    __tablename__ = "ecosystem_index"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date = Column(DateTime, unique=True, index=True)
    sector_density = Column(JSON)
    total_vendors = Column(Integer)
    active_vendors = Column(Integer)
    avg_vendor_score = Column(Float)
    total_enterprises = Column(Integer)
    active_procurements = Column(Integer)
    gov_activity = Column(Integer)
    triggers_24h = Column(Integer)
    avg_trigger_score = Column(Float)
    procurement_heatmap = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

class EnterpriseLead(Base):
    __tablename__ = "enterprise_leads"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), unique=True)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("enterprise_profiles.id"), nullable=True)
    score = Column(Integer, index=True)
    triggered = Column(Boolean, default=False)
    priority = Column(SQLEnum(LeadPriority), default=LeadPriority.MEDIUM, index=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    correlation_id = Column(String(255), nullable=True)
    status = Column(SQLEnum(LeadStatus), default=LeadStatus.NEW, index=True)
    deal_value = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    triggered_at = Column(DateTime, nullable=True)

class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id = Column(UUID(as_uuid=True), ForeignKey("enterprise_leads.id", ondelete="CASCADE"))
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title = Column(String(255))
    scheduled_at = Column(DateTime, index=True)
    duration = Column(Integer, default=30)
    status = Column(SQLEnum(MeetingStatus), default=MeetingStatus.SCHEDULED, index=True)
    meeting_url = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VendorSector(Base):
    __tablename__ = "vendor_sectors"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    sector = Column(String(255), index=True)

class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type = Column(String(100), index=True)
    description = Column(String(500))
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class GeBizActivity(Base):
    __tablename__ = "gebiz_activities"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), index=True)
    tender_id = Column(String(255), nullable=True)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    status = Column(String(100))
    correlation_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class LeadCapture(Base):
    __tablename__ = "lead_captures"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), index=True)
    company = Column(String(255), nullable=True)
    uen = Column(String(50), nullable=True)
    avg_tender_value = Column(Integer)
    tenders_per_year = Column(Integer)
    win_rate = Column(Integer)
    lost_revenue = Column(Integer)
    sectors_json = Column("sectors", JSON, nullable=True)
    readiness_score = Column(Integer, default=0)
    correlation_id = Column(String(255), nullable=True)
    converted = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
