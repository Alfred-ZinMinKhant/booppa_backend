

# ============================================================
# Extracted from models.py
# ============================================================
import uuid
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, ForeignKey,
                        Integer, String, Text)
from sqlalchemy.dialects.postgresql import UUID

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # V6 Additions for Vendor features
    role = Column(String(50), default="VENDOR")
    company = Column(String(255), nullable=True)
    uen = Column(String(50), unique=True, nullable=True)
    plan = Column(String(50), default="free", nullable=False, server_default="free")
    temp_password = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)
    subscription_tier = Column(String(50), nullable=True)
    subscription_started_at = Column(DateTime, nullable=True)
    # Day of month (1-31) the buyer's monthly cycle should fire. Set at
    # activation from `subscription_started_at.day` — UNCAPPED. The cron
    # filter handles short months: on the last day of a 28/29/30-day month,
    # subscribers with anniversary_day >= today.day all fire (so a Jan-31
    # subscriber gets their cycle on Feb 28, Apr 30, etc.). Replaces
    # calendar-1st delivery — each subscriber's cycle lands on their own day.
    subscription_anniversary_day = Column(Integer, nullable=True)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    website = Column(String(500), nullable=True)
    industry = Column(String(100), nullable=True)
    company_description = Column(Text, nullable=True)

    # Notarization credits granted by bundle purchases (compliance_evidence_pack,
    # vendor_trust_pack, rfp_accelerator, enterprise_bid_kit). Decremented when the
    # user uploads a document at /notarize. Distinct from monthly enterprise credits
    # (NotarizationCredit table) which are subscription-based.
    notarization_credits = Column(Integer, default=0, nullable=False, server_default="0")
    # Set to True when the user purchases a Compliance Evidence Pack — triggers
    # cover-sheet generation on last credit redemption or via the manual trigger
    # endpoint. Cleared after the cover sheet is queued.
    pending_cover_sheet = Column(Boolean, default=False, nullable=False, server_default="false")
    # Set to True once the RFP Complete kit finishes generating for a Compliance
    # Evidence Pack purchase. Combined with the PDPA "completed" Report row, this
    # tells _maybe_fire_cover_sheet that both inputs to Section 4/5 are ready.
    compliance_evidence_rfp_ready = Column(Boolean, default=False, nullable=False, server_default="false")
    # Dedicated 1-credit pool for the Compliance Evidence Pack signed-cover-sheet
    # upload. Kept separate from `notarization_credits` so the Cover Sheet workflow
    # can never be cannibalised by other bundle uploads.
    compliance_evidence_credits = Column(Integer, default=0, nullable=False, server_default="0")
    # True once the user uploads their signed Cover Sheet PDF and we anchor it.
    signed_cover_sheet_uploaded = Column(Boolean, default=False, nullable=False, server_default="false")
    # Vendor Pro: opt-out from being counted in TenderCheckLookup. When True,
    # /tender-check skips the lookup insert for this user. Default False so the
    # competitor-awareness signal has data from day one.
    tender_lookup_opt_out = Column(Boolean, default=False, nullable=False, server_default="false")
    # Multi-subsidiary (Pro Suite): if set, this user is a child of another tenant.
    # Parent users see aggregate data for all children + manage their lifecycle.
    parent_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class Report(Base):
    __tablename__ = "reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    framework = Column(String(100), nullable=False)
    company_name = Column(String(255), nullable=False)
    company_website = Column(String(500), nullable=True)
    assessment_data = Column(JSON, nullable=False)

    # Processing status
    status = Column(String(50), default="pending", index=True)

    # Blockchain evidence
    audit_hash = Column(String(64), nullable=True)
    tx_hash = Column(String(66), nullable=True, index=True)

    # Storage
    s3_url = Column(Text, nullable=True)
    file_key = Column(String(500), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # AI Narrative
    ai_narrative = Column(Text, nullable=True)
    ai_model_used = Column(String(100), nullable=True)


class AuditChainEvent(Base):
    __tablename__ = "audit_chain_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(
        UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False, index=True
    )
    action = Column(String(100), nullable=False, index=True)
    actor = Column(String(255), nullable=False)
    hash_prev = Column(String(64), nullable=False)
    hash = Column(String(64), nullable=False, index=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class TaskLock(Base):
    __tablename__ = "task_locks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(String(255), unique=True, nullable=False, index=True)
    locked_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class ConsentLog(Base):
    __tablename__ = "consent_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    ip_anonymized = Column(String(64), nullable=True)
    consent_status = Column(String(50), nullable=False, index=True)
    policy_version = Column(String(100), nullable=True)
    # `metadata` is a reserved attribute name on the Declarative base, so
    # store JSON metadata in the database column named "metadata" but expose
    # it on the model as `metadata_json` to avoid conflicts.
    metadata_json = Column("metadata", JSON, nullable=True)


class HardenedConsent(Base):
    __tablename__ = "hardened_consents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_email = Column(String(255), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    legal_version = Column(String(100), nullable=False, default="v17_Hardened")
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


class DemoBooking(Base):
    __tablename__ = "demo_bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slot_id = Column(String(32), nullable=False, index=True)
    slot_date = Column(Date, nullable=False, index=True)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)

    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False, index=True)
    customer_phone = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    status = Column(String(32), default="confirmed", index=True)
    booking_token = Column(String(32), unique=True, index=True, nullable=False)
    source = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(String(50), unique=True, nullable=False, index=True)
    tracking_token = Column(String(64), unique=True, nullable=False, index=True)

    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    subject = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)

    status = Column(String(32), default="open", index=True)
    priority = Column(String(32), default="medium", index=True)
    assigned_to = Column(String(255), nullable=True)

    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupportTicketReply(Base):
    __tablename__ = "support_ticket_replies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(String(50), nullable=False, index=True)
    author = Column(String(255), nullable=False)
    author_type = Column(String(20), nullable=False, default="staff")
    message = Column(Text, nullable=False)
    is_internal = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ResourceItem(Base):
    __tablename__ = "resource_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    href = Column(String(500), nullable=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProcessedWebhookEvent(Base):
    """Idempotency guard: records every Stripe event ID we have processed."""

    __tablename__ = "processed_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(255), unique=True, nullable=False, index=True)
    event_type = Column(String(100), nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow, index=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    stripe_subscription_id = Column(
        String(255), unique=True, nullable=False, index=True
    )
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    product_type = Column(String(100), nullable=True)
    status = Column(String(50), nullable=True, index=True)
    current_period_end = Column(DateTime, nullable=True)
    # `metadata` is a reserved attribute on Declarative base; expose as `metadata_json`
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Import CSP Compliance Pack models (organisations, profiles, clients, CDD/EDD/STR,
# nominees, UBOs, AML programme, calendar, training, blockchain evidence, v3 legal layer).
# Import V12 Enterprise extensions
# Import V6 extensions so Alembic picks them up correctly
# Import V8 extensions (VendorStatusSnapshot, ScoreSnapshot, NotarizationMetadata,
# RfpRequirement, RfpRequirementFlag)
# Import V11 extensions (ComplianceRequirement, ManagedVendor)
# Import V12 (ApiKey, PendingRfpIntake, VendorEvaluationFramework) so Alembic
# metadata + create_all see them even if no API module has imported v12 yet.
# Import V13 (EvidencePack — BCEP compliance evidence pack) for the same reason.

# ============================================================
# Extracted from models_csp.py
# ============================================================
"""
Booppa CSP Compliance Pack — SQLAlchemy Models (v2)
FIX #1 APPLIED: All PII fields now use EncryptedString / EncryptedText
TypeDecorators for transparent application-level AES encryption.

Encrypted fields:
  - CspCddRecord.individual_nric_or_passport
  - CspCddRecord.individual_address
  - CspNomineeDirector.nominee_nric_or_passport
  - CspNomineeDirector.nominee_address
  - CspNomineeDirector.nominator_id
  - CspNomineeShareholder.nominee_nric_or_passport
  - CspNomineeShareholder.nominator_id
  - CspBeneficialOwner.ubo_nric_or_passport
  - CspBeneficialOwner.ubo_address
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Enum, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

# Shared declarative Base — registers CSP tables in the single Alembic metadata
from app.core.db import Base
# Import encryption TypeDecorators (Fernet AES at the application layer)
from app.core.encryption import EncryptedString, EncryptedText


def utcnow():
    return datetime.now(timezone.utc)


# ── CSP ORGANISATION (tenancy) ────────────────────────────────────────────────

class CspOrganisation(Base):
    """
    Tenant anchor for the CSP Compliance Pack. Every CSP profile, client, and
    compliance record hangs off an organisation. Booppa users are linked to an
    organisation via CspOrgMembership; the router's auth adapter resolves (and
    auto-provisions) the caller's organisation, exposing its id as ``org_id``.
    """
    __tablename__ = "csp_organisations"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name          = Column(String(255), nullable=False)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    plan          = Column(String(50), default="full")
    monthly_fee_sgd = Column(Float, default=299.0)
    # Access gate. Set by the Stripe webhook on a paid CSP purchase; the router's
    # auth adapter blocks all CSP endpoints with 402 until status == "active".
    subscription_status = Column(String(20), default="inactive")   # active | inactive | cancelled
    billing_type        = Column(String(20), nullable=True)        # subscription | one_time

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    memberships = relationship("CspOrgMembership", back_populates="organisation",
                               cascade="all, delete-orphan")


class CspOrgMembership(Base):
    """Links a Booppa user to a CSP organisation, with a role for require_role()."""
    __tablename__ = "csp_org_memberships"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id    = Column(UUID(as_uuid=True), ForeignKey("csp_organisations.id"), nullable=False, index=True)
    user_id   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    role      = Column(String(50), default="csp_admin")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    organisation = relationship("CspOrganisation", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_csp_org_member"),
    )


# ── ENUMS ────────────────────────────────────────────────────────────────────

class RegistrationStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED   = "submitted"
    APPROVED    = "approved"
    RENEWAL_DUE = "renewal_due"
    SUSPENDED   = "suspended"
    REVOKED     = "revoked"


class RiskRating(str, enum.Enum):
    LOW       = "low"
    MEDIUM    = "medium"
    HIGH      = "high"
    VERY_HIGH = "very_high"


class CddStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"
    EXPIRED     = "expired"
    PENDING_EDD = "pending_edd"


class StrDecision(str, enum.Enum):
    FILED      = "filed"
    NOT_FILED  = "not_filed"
    PENDING    = "pending"
    ESCALATED  = "escalated"


class NomineeAssessment(str, enum.Enum):
    NOT_ASSESSED = "not_assessed"
    FIT_PROPER   = "fit_proper"
    NOT_FIT      = "not_fit"
    UNDER_REVIEW = "under_review"


class TrainingStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    OVERDUE     = "overdue"
    EXPIRED     = "expired"


class CompliancePillar(str, enum.Enum):
    ACRA_REGISTRATION    = "acra_registration"
    AML_CFT_PROGRAMME    = "aml_cft_programme"
    CDD                  = "cdd"
    EDD                  = "edd"
    STR                  = "str"
    NOMINEE_MANAGEMENT   = "nominee_management"
    BENEFICIAL_OWNERSHIP = "beneficial_ownership"
    PDPA_NRIC            = "pdpa_nric"
    STAFF_TRAINING       = "staff_training"


# ── CSP PROFILE ──────────────────────────────────────────────────────────────

class CspProfile(Base):
    __tablename__ = "csp_profiles"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("csp_organisations.id"), nullable=False, unique=True, index=True)

    legal_name          = Column(String(255), nullable=False)
    uen                 = Column(String(20), nullable=False, unique=True)
    registered_address  = Column(Text())
    business_email      = Column(String(255))
    business_phone      = Column(String(50))

    acra_reg_status     = Column(Enum(RegistrationStatus), default=RegistrationStatus.NOT_STARTED)
    acra_reg_number     = Column(String(50))
    acra_reg_date       = Column(DateTime(timezone=True))
    acra_renewal_date   = Column(DateTime(timezone=True))
    acra_licence_type   = Column(String(100))

    rqi_name                 = Column(String(255))
    rqi_qualification        = Column(String(255))
    rqi_training_completed   = Column(Boolean, default=False)
    rqi_training_date        = Column(DateTime(timezone=True))
    rqi_acra_registration_no = Column(String(50))

    offers_company_formation    = Column(Boolean, default=False)
    offers_nominee_director     = Column(Boolean, default=False)
    offers_nominee_shareholder  = Column(Boolean, default=False)
    offers_registered_address   = Column(Boolean, default=False)
    offers_corp_secretarial     = Column(Boolean, default=False)
    offers_shelf_company        = Column(Boolean, default=False)

    aml_programme_exists    = Column(Boolean, default=False)
    aml_programme_version   = Column(String(20))
    aml_programme_reviewed  = Column(DateTime(timezone=True))
    aml_compliance_officer  = Column(String(255))

    overall_compliance_score = Column(Float, default=0.0)
    last_scored_at           = Column(DateTime(timezone=True))

    csp_pack_tier    = Column(String(20), default="full")
    amount_paid_sgd  = Column(Float)
    monthly_fee_sgd  = Column(Float, default=299.0)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    clients          = relationship("CspClient",            back_populates="csp", cascade="all, delete-orphan")
    nominees         = relationship("CspNomineeDirector",   back_populates="csp", cascade="all, delete-orphan")
    nom_shareholders = relationship("CspNomineeShareholder",back_populates="csp", cascade="all, delete-orphan")
    str_reports      = relationship("CspStrReport",         back_populates="csp", cascade="all, delete-orphan")
    aml_programme    = relationship("CspAmlProgramme",      back_populates="csp", cascade="all, delete-orphan")
    training_records = relationship("CspStaffTraining",     back_populates="csp", cascade="all, delete-orphan")
    calendar         = relationship("CspComplianceCalendar",back_populates="csp", cascade="all, delete-orphan")
    evidence         = relationship("CspBlockchainEvidence",back_populates="csp", cascade="all, delete-orphan")


# ── CSP CLIENT ────────────────────────────────────────────────────────────────

class CspClient(Base):
    __tablename__ = "csp_clients"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id      = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    client_type = Column(String(30), nullable=False)
    legal_name  = Column(String(255), nullable=False)
    uen_or_reg_no      = Column(String(50))
    country_of_inc     = Column(String(100))
    registered_address = Column(Text())
    contact_name       = Column(String(255))
    contact_email      = Column(String(255))
    contact_phone      = Column(String(50))
    services_provided  = Column(JSONB)
    onboarded_at       = Column(DateTime(timezone=True))
    offboarded_at      = Column(DateTime(timezone=True))
    is_active          = Column(Boolean, default=True)
    risk_rating        = Column(Enum(RiskRating), default=RiskRating.MEDIUM)
    risk_rationale     = Column(Text())
    is_pep             = Column(Boolean, default=False)
    pep_details        = Column(Text())
    high_risk_country  = Column(Boolean, default=False)
    country_risk_basis = Column(String(255))
    cdd_status         = Column(Enum(CddStatus), default=CddStatus.NOT_STARTED, index=True)
    cdd_completed_at   = Column(DateTime(timezone=True))
    cdd_next_review    = Column(DateTime(timezone=True))
    edd_required       = Column(Boolean, default=False)
    edd_trigger        = Column(String(50))
    has_nominee_director    = Column(Boolean, default=False)
    has_nominee_shareholder = Column(Boolean, default=False)
    is_remote_onboarding    = Column(Boolean, default=False)
    video_call_completed    = Column(Boolean, default=False)
    video_call_date         = Column(DateTime(timezone=True))
    video_call_conducted_by = Column(String(255))
    str_filed  = Column(Boolean, default=False)
    str_count  = Column(Integer, default=0)
    # Sanctions screening result (cached)
    sanctions_screened      = Column(Boolean, default=False)
    sanctions_clear         = Column(Boolean)
    sanctions_screened_at   = Column(DateTime(timezone=True))
    sanctions_hits          = Column(JSONB)   # [{list, name, entry_id}]
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp               = relationship("CspProfile",      back_populates="clients")
    cdd_records       = relationship("CspCddRecord",    back_populates="client", cascade="all, delete-orphan")
    edd_records       = relationship("CspEddRecord",    back_populates="client", cascade="all, delete-orphan")
    beneficial_owners = relationship("CspBeneficialOwner", back_populates="client", cascade="all, delete-orphan")
    risk_assessments  = relationship("CspRiskAssessment",  back_populates="client", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_csp_clients_csp_status", "csp_id", "cdd_status"),
        Index("ix_csp_clients_risk",       "csp_id", "risk_rating"),
    )


# ── CDD RECORD ────────────────────────────────────────────────────────────────

class CspCddRecord(Base):
    """
    FIX #1: individual_nric_or_passport and individual_address
    are now encrypted at rest using EncryptedString / EncryptedText.
    """
    __tablename__ = "csp_cdd_records"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id   = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False, index=True)
    csp_id      = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False)
    review_type = Column(String(30))

    # ── ENCRYPTED PII FIELDS ─────────────────────────────────────────────
    individual_full_name         = Column(String(255))                  # Not encrypted — name is searchable
    individual_nric_or_passport  = Column(EncryptedString(300))         # ENCRYPTED
    individual_dob               = Column(String(20))                   # Not encrypted — low sensitivity
    individual_nationality       = Column(String(100))
    individual_address           = Column(EncryptedText())              # ENCRYPTED
    # ── END ENCRYPTED ────────────────────────────────────────────────────

    id_doc_type             = Column(String(50))
    id_doc_verified         = Column(Boolean, default=False)
    id_doc_expiry           = Column(String(20))
    id_verification_method  = Column(String(50))
    corp_registration_verified  = Column(Boolean, default=False)
    corp_constitution_obtained  = Column(Boolean, default=False)
    corp_directors_identified   = Column(Boolean, default=False)
    corp_shareholders_identified = Column(Boolean, default=False)
    business_purpose        = Column(Text())
    source_of_funds         = Column(Text())
    source_of_wealth        = Column(Text())
    expected_transactions   = Column(Text())
    non_face_to_face        = Column(Boolean, default=False)
    video_call_completed    = Column(Boolean, default=False)
    video_call_recording_ref = Column(String(255))
    sanctions_screened      = Column(Boolean, default=False)
    sanctions_clear         = Column(Boolean)
    sanctions_screen_date   = Column(DateTime(timezone=True))
    sanctions_screen_provider = Column(String(100))
    pep_screening_done      = Column(Boolean, default=False)
    pep_result              = Column(String(50))
    adverse_media_checked   = Column(Boolean, default=False)
    status          = Column(Enum(CddStatus), default=CddStatus.IN_PROGRESS)
    completed_by    = Column(String(255))
    completed_at    = Column(DateTime(timezone=True))
    next_review_date = Column(DateTime(timezone=True))
    failure_reason  = Column(Text())
    evidence_files  = Column(JSONB)
    blockchain_tx_hash   = Column(String(66))
    blockchain_timestamp = Column(DateTime(timezone=True))
    polygonscan_url      = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    client = relationship("CspClient", back_populates="cdd_records")

    __table_args__ = (
        Index("ix_csp_cdd_client_date", "client_id", "completed_at"),
    )


# ── EDD RECORD ────────────────────────────────────────────────────────────────

class CspEddRecord(Base):
    __tablename__ = "csp_edd_records"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id   = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False, index=True)
    csp_id      = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False)
    trigger     = Column(String(50), nullable=False)
    trigger_detail = Column(Text())
    senior_mgmt_approval      = Column(Boolean, default=False)
    senior_mgmt_approver      = Column(String(255))
    senior_mgmt_approval_date = Column(DateTime(timezone=True))
    enhanced_source_of_funds  = Column(Boolean, default=False)
    enhanced_source_of_wealth = Column(Boolean, default=False)
    enhanced_business_purpose = Column(Boolean, default=False)
    enhanced_sanctions_screen = Column(Boolean, default=False)
    ongoing_monitoring_freq   = Column(String(50))
    pep_name         = Column(String(255))
    pep_position     = Column(String(255))
    pep_country      = Column(String(100))
    pep_relationship = Column(String(100))
    edd_conclusion   = Column(Text())
    risk_accepted    = Column(Boolean)
    risk_accepted_by = Column(String(255))
    conditions_imposed = Column(Text())
    status           = Column(String(30), default="in_progress")
    completed_at     = Column(DateTime(timezone=True))
    evidence_files   = Column(JSONB)
    blockchain_tx_hash = Column(String(66))
    polygonscan_url  = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    client = relationship("CspClient", back_populates="edd_records")


# ── STR REPORT ────────────────────────────────────────────────────────────────

class CspStrReport(Base):
    __tablename__ = "csp_str_reports"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id      = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    client_id   = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=True)
    trigger_type    = Column(String(100))
    trigger_detail  = Column(Text(), nullable=False)
    amount_involved = Column(Float)
    currency        = Column(String(10))
    transaction_date = Column(DateTime(timezone=True))
    decision         = Column(Enum(StrDecision), nullable=False)
    decision_by      = Column(String(255))
    decision_date    = Column(DateTime(timezone=True))
    decision_rationale = Column(Text(), nullable=False)
    stro_reference   = Column(String(100))
    stro_filed_date  = Column(DateTime(timezone=True))
    stro_filed_by    = Column(String(255))
    client_notified  = Column(Boolean, default=False)   # ALWAYS False — tipping-off protection
    service_declined = Column(Boolean, default=False)
    escalated_to_senior_mgmt = Column(Boolean, default=False)
    senior_mgmt_name         = Column(String(255))
    escalation_date          = Column(DateTime(timezone=True))
    evidence_files     = Column(JSONB)
    blockchain_tx_hash = Column(String(66))
    polygonscan_url    = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp    = relationship("CspProfile", back_populates="str_reports")
    client = relationship("CspClient")

    __table_args__ = (
        Index("ix_csp_str_date", "csp_id", "decision_date"),
    )


# ── NOMINEE DIRECTOR ──────────────────────────────────────────────────────────

class CspNomineeDirector(Base):
    """
    FIX #1: nominee_nric_or_passport, nominee_address, nominator_id
    are now encrypted at rest.
    """
    __tablename__ = "csp_nominee_directors"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id    = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    client_id = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False)

    nominee_full_name         = Column(String(255), nullable=False)
    nominee_nric_or_passport  = Column(EncryptedString(300))    # ENCRYPTED
    nominee_nationality       = Column(String(100))
    nominee_address           = Column(EncryptedText())          # ENCRYPTED
    nominator_name            = Column(String(255), nullable=False)
    nominator_id              = Column(EncryptedString(300))     # ENCRYPTED
    nominator_relationship    = Column(Text())
    company_name              = Column(String(255))
    company_uen               = Column(String(20))
    appointment_date          = Column(DateTime(timezone=True))
    cessation_date            = Column(DateTime(timezone=True))
    is_active                 = Column(Boolean, default=True)
    assessment_status         = Column(Enum(NomineeAssessment), default=NomineeAssessment.NOT_ASSESSED)
    assessment_date           = Column(DateTime(timezone=True))
    assessed_by               = Column(String(255))
    criminal_check_done       = Column(Boolean, default=False)
    bankruptcy_check_done     = Column(Boolean, default=False)
    director_history_check    = Column(Boolean, default=False)
    assessment_outcome        = Column(Text())
    assessment_notes          = Column(Text())
    acra_disclosed            = Column(Boolean, default=False)
    acra_filing_date          = Column(DateTime(timezone=True))
    acra_filing_ref           = Column(String(100))
    last_reviewed             = Column(DateTime(timezone=True))
    next_review               = Column(DateTime(timezone=True))
    evidence_files            = Column(JSONB)
    blockchain_tx_hash        = Column(String(66))
    polygonscan_url           = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp    = relationship("CspProfile", back_populates="nominees")
    client = relationship("CspClient")


# ── NOMINEE SHAREHOLDER ───────────────────────────────────────────────────────

class CspNomineeShareholder(Base):
    """FIX #1: nominee_nric_or_passport and nominator_id are encrypted."""
    __tablename__ = "csp_nominee_shareholders"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id    = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    client_id = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False)

    nominee_full_name        = Column(String(255), nullable=False)
    nominee_nric_or_passport = Column(EncryptedString(300))   # ENCRYPTED
    nominee_nationality      = Column(String(100))
    nominator_name           = Column(String(255), nullable=False)
    nominator_id             = Column(EncryptedString(300))   # ENCRYPTED
    shares_held              = Column(String(100))
    share_percentage         = Column(Float)
    company_name             = Column(String(255))
    company_uen              = Column(String(20))
    appointment_date         = Column(DateTime(timezone=True))
    cessation_date           = Column(DateTime(timezone=True))
    is_active                = Column(Boolean, default=True)
    acra_disclosed           = Column(Boolean, default=False)
    acra_filing_date         = Column(DateTime(timezone=True))
    acra_filing_ref          = Column(String(100))
    evidence_files           = Column(JSONB)
    blockchain_tx_hash       = Column(String(66))
    polygonscan_url          = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp    = relationship("CspProfile", back_populates="nom_shareholders")
    client = relationship("CspClient")


# ── BENEFICIAL OWNER ──────────────────────────────────────────────────────────

class CspBeneficialOwner(Base):
    """FIX #1: ubo_nric_or_passport and ubo_address are encrypted."""
    __tablename__ = "csp_beneficial_owners"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False, index=True)
    csp_id    = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False)

    ubo_full_name             = Column(String(255), nullable=False)
    ubo_nric_or_passport      = Column(EncryptedString(300))   # ENCRYPTED
    ubo_nationality           = Column(String(100))
    ubo_dob                   = Column(String(20))
    ubo_address               = Column(EncryptedText())         # ENCRYPTED
    ubo_country_of_residence  = Column(String(100))
    ownership_percentage      = Column(Float)
    control_mechanism         = Column(String(255))
    is_pep                    = Column(Boolean, default=False)
    is_sanctioned             = Column(Boolean, default=False)
    identity_verified         = Column(Boolean, default=False)
    verification_method       = Column(String(100))
    verification_date         = Column(DateTime(timezone=True))
    verified_by               = Column(String(255))
    verification_doc          = Column(String(255))
    last_updated              = Column(DateTime(timezone=True))
    next_review               = Column(DateTime(timezone=True))
    evidence_files            = Column(JSONB)
    blockchain_tx_hash        = Column(String(66))
    polygonscan_url           = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    client = relationship("CspClient", back_populates="beneficial_owners")


# ── AML PROGRAMME ─────────────────────────────────────────────────────────────

class CspAmlProgramme(Base):
    __tablename__ = "csp_aml_programme"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id    = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    version   = Column(Integer, default=1)
    is_current = Column(Boolean, default=True)
    status    = Column(String(30), default="draft")
    risk_assessment_section    = Column(Text())
    cdd_procedures_section     = Column(Text())
    edd_procedures_section     = Column(Text())
    str_procedures_section     = Column(Text())
    record_keeping_section     = Column(Text())
    training_policy_section    = Column(Text())
    governance_section         = Column(Text())
    nominee_procedures_section = Column(Text())
    approved_by      = Column(String(255))
    approved_at      = Column(DateTime(timezone=True))
    next_review_date = Column(DateTime(timezone=True))
    generated_by_model  = Column(String(100), default="deepseek-chat")
    generation_cost_usd = Column(Float)
    s3_key           = Column(String(500))
    pdf_hash         = Column(String(64))
    blockchain_tx_hash    = Column(String(66))
    blockchain_timestamp  = Column(DateTime(timezone=True))
    polygonscan_url       = Column(String(500))
    generated_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at   = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp = relationship("CspProfile", back_populates="aml_programme")

    __table_args__ = (
        UniqueConstraint("csp_id", "version", name="uq_aml_csp_version"),
    )


# ── RISK ASSESSMENT ───────────────────────────────────────────────────────────

class CspRiskAssessment(Base):
    __tablename__ = "csp_risk_assessments"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id       = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False, index=True)
    csp_id          = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False)
    assessment_date = Column(DateTime(timezone=True), default=utcnow)
    assessed_by     = Column(String(255))
    country_risk    = Column(Integer)
    industry_risk   = Column(Integer)
    product_risk    = Column(Integer)
    delivery_risk   = Column(Integer)
    customer_risk   = Column(Integer)
    transaction_risk = Column(Integer)
    composite_score = Column(Float)
    risk_rating     = Column(Enum(RiskRating))
    edd_required    = Column(Boolean, default=False)
    review_frequency = Column(String(50))
    next_review_date = Column(DateTime(timezone=True))
    notes           = Column(Text())
    blockchain_tx_hash = Column(String(66))
    polygonscan_url    = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)

    client = relationship("CspClient", back_populates="risk_assessments")


# ── COMPLIANCE CALENDAR ───────────────────────────────────────────────────────

class CspComplianceCalendar(Base):
    __tablename__ = "csp_compliance_calendar"

    id       = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id   = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    pillar   = Column(String(50), nullable=False)
    title    = Column(String(255), nullable=False)
    description      = Column(Text())
    due_date         = Column(DateTime(timezone=True), nullable=False)
    frequency        = Column(String(30))
    legal_basis      = Column(String(255))
    penalty_if_missed = Column(String(255))
    status           = Column(String(30), default="pending")
    completed_at     = Column(DateTime(timezone=True))
    completed_by     = Column(String(255))
    evidence_ref     = Column(String(255))
    alert_30_days_sent = Column(Boolean, default=False)
    alert_14_days_sent = Column(Boolean, default=False)
    alert_7_days_sent  = Column(Boolean, default=False)
    alert_overdue_sent = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp = relationship("CspProfile", back_populates="calendar")

    __table_args__ = (
        Index("ix_csp_cal_due",    "csp_id", "due_date"),
        Index("ix_csp_cal_status", "csp_id", "status"),
    )


# ── STAFF TRAINING ────────────────────────────────────────────────────────────

class CspStaffTraining(Base):
    __tablename__ = "csp_staff_training"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id        = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    staff_name    = Column(String(255), nullable=False)
    staff_role    = Column(String(100))
    is_rqi        = Column(Boolean, default=False)
    training_type = Column(String(100))
    training_title = Column(String(255))
    provider      = Column(String(255))
    training_date = Column(DateTime(timezone=True))
    completion_date = Column(DateTime(timezone=True))
    expiry_date   = Column(DateTime(timezone=True))
    status        = Column(Enum(TrainingStatus), default=TrainingStatus.NOT_STARTED)
    score         = Column(Integer)
    certificate_ref = Column(String(255))
    evidence_s3_key = Column(String(500))
    blockchain_tx_hash = Column(String(66))
    polygonscan_url    = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    csp = relationship("CspProfile", back_populates="training_records")

    __table_args__ = (
        Index("ix_csp_training_staff", "csp_id", "staff_name"),
    )


# ── BLOCKCHAIN EVIDENCE ───────────────────────────────────────────────────────

class CspBlockchainEvidence(Base):
    __tablename__ = "csp_blockchain_evidence"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id        = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    record_type   = Column(String(50), nullable=False)
    record_id     = Column(UUID(as_uuid=True))
    record_title  = Column(String(255))
    related_client = Column(String(255))
    document_hash  = Column(String(64), nullable=False)
    tx_hash        = Column(String(66), nullable=False)
    block_number   = Column(Integer)
    network        = Column(String(50), default="polygon-mainnet")
    blockchain_timestamp = Column(DateTime(timezone=True))
    polygonscan_url      = Column(String(500))
    gas_used             = Column(Integer)
    metadata_payload     = Column(String(500))
    created_at = Column(DateTime(timezone=True), default=utcnow)

    csp = relationship("CspProfile", back_populates="evidence")

    __table_args__ = (
        Index("ix_csp_evidence_type", "csp_id", "record_type"),
        Index("ix_csp_evidence_tx",   "tx_hash"),
    )


# ── TOS ACCEPTANCE (v3 — Layer 3) ─────────────────────────────────────────────

class CspTosAcceptance(Base):
    """
    Records the CSP's explicit ToS acceptance, including the explicit liability
    cap and the AI-specific clauses.
    Notarized on Polygon for evidentiary weight.
    """
    __tablename__ = "csp_tos_acceptances"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ToS is accepted at the organisation level BEFORE a CspProfile exists, so this
    # references csp_organisations.id (the value the router exposes as ``org_id``).
    csp_id          = Column(UUID(as_uuid=True), ForeignKey("csp_organisations.id"), nullable=False, index=True)
    user_id         = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    user_email      = Column(String(255), nullable=False)

    tos_version     = Column(String(20), nullable=False, default="1.0")
    accepted_at     = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    ip_address      = Column(String(45))
    user_agent      = Column(String(500))

    # Individual checkboxes — each must be True
    checkbox_ai_disclaimer        = Column(Boolean, nullable=False, default=False)
    checkbox_data_accuracy        = Column(Boolean, nullable=False, default=False)
    checkbox_sanctions_limitation = Column(Boolean, nullable=False, default=False)
    checkbox_regulatory_change    = Column(Boolean, nullable=False, default=False)
    checkbox_liability_cap        = Column(Boolean, nullable=False, default=False)
    # Explicit liability-cap text shown and confirmed
    liability_cap_amount_sgd      = Column(Float, nullable=False)
    liability_cap_text_shown      = Column(Text(), nullable=False)

    # Blockchain proof
    content_hash        = Column(String(64))
    blockchain_tx_hash  = Column(String(66))
    polygonscan_url     = Column(String(500))
    notarized_at        = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_csp_tos_csp_id",   "csp_id"),
        Index("ix_csp_tos_user_id",  "user_id"),
        UniqueConstraint("csp_id", "tos_version", name="uq_csp_tos_version"),
    )


# ── PROGRAMME APPROVAL ATTESTATION (v3 — Layer 1) ────────────────────────────

class CspProgrammeAttestation(Base):
    """
    Records the CSP's explicit attestation at the time of approving the
    AML/CFT Programme document. Includes the three mandatory declarations.
    Non-bypassable: Programme approval is blocked without it.
    """
    __tablename__ = "csp_programme_attestations"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    programme_id   = Column(UUID(as_uuid=True), ForeignKey("csp_aml_programme.id"), nullable=False, unique=True)
    csp_id         = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    approved_by    = Column(String(255), nullable=False)
    approved_at    = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    # The three declarations — all must be True
    declaration_content_accurate        = Column(Boolean, nullable=False, default=False)
    declaration_legal_advice_considered = Column(Boolean, nullable=False, default=False)
    declaration_sole_responsible        = Column(Boolean, nullable=False, default=False)

    # Exact text of the declarations shown to the user (for future proof)
    declaration_text_shown = Column(Text(), nullable=False)

    # Blockchain proof
    content_hash       = Column(String(64))
    blockchain_tx_hash = Column(String(66))
    polygonscan_url    = Column(String(500))
    notarized_at       = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=utcnow)


# ── RISK CLASSIFICATION INPUT NOTARIZATION (v3 — Layer 2) ─────────────────────

class CspRiskClassificationAudit(Base):
    """
    Notarizes the time and content of a risk_rating assignment made by the
    CSP for a client. Proves the classification was confirmed by the CSP,
    not generated autonomously by Booppa.

    Created every time the CSP changes a client's risk_rating.
    """
    __tablename__ = "csp_risk_classification_audits"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    csp_id      = Column(UUID(as_uuid=True), ForeignKey("csp_profiles.id"), nullable=False, index=True)
    client_id   = Column(UUID(as_uuid=True), ForeignKey("csp_clients.id"), nullable=False, index=True)
    classified_by = Column(String(255), nullable=False)
    classified_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    risk_rating_assigned     = Column(String(20), nullable=False)
    risk_rating_previous     = Column(String(20))
    risk_rationale           = Column(Text())

    # Snapshot of the risk flags at the time of classification
    is_pep_at_classification      = Column(Boolean, default=False)
    high_risk_country_at_class    = Column(Boolean, default=False)
    sanctions_clear_at_class      = Column(Boolean)
    edd_required_at_class         = Column(Boolean, default=False)
    # Additional flags provided by the CSP as rationale
    additional_risk_flags         = Column(JSONB)

    # Blockchain proof
    content_hash       = Column(String(64))
    blockchain_tx_hash = Column(String(66))
    polygonscan_url    = Column(String(500))
    notarized_at       = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_risk_audit_client", "client_id"),
        Index("ix_risk_audit_csp",    "csp_id"),
    )


# ============================================================
# Extracted from models_enterprise.py
# ============================================================
"""
Enterprise Package models — V12
Organisations, SSO, Webhooks, MAS TRM, White-label
"""
import uuid
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.db import Base


class Organisation(Base):
    __tablename__ = "organisations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    tier = Column(String(50), default="standard")          # standard | pro | custom
    # Business sector (fintech | healthcare | …). Drives sector-priority ordering
    # of the 13 MAS TRM domains in the baseline + workspace. NULL → canonical order.
    sector = Column(String(50), nullable=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    # Seat cap. NULL = unlimited (Suites + legacy Enterprise + Buyer Enterprise).
    # Source of truth is PLAN_TO_MAX_SEATS — set on org creation and refreshed
    # by the subscription webhook on plan change.
    max_seats = Column(Integer, nullable=True)
    # Buyer's default vendor-scoring profile (VendorEvaluationFramework). NULL =
    # use the built-in DEFAULT weights. Plain UUID column (no hard FK) to avoid a
    # circular dependency with models_v12; resolution is by id lookup.
    active_framework_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Subsidiary(Base):
    __tablename__ = "subsidiaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(255), nullable=False)
    uen = Column(String(50))
    country = Column(String(100), default="Singapore")
    created_at = Column(DateTime, default=datetime.utcnow)


class OrganisationMember(Base):
    __tablename__ = "organisation_members"
    __table_args__ = (UniqueConstraint("organisation_id", "user_id"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role = Column(String(50), default="member")            # owner | admin | member
    created_at = Column(DateTime, default=datetime.utcnow)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    url = Column(Text, nullable=False)
    secret = Column(String(128), nullable=False)           # used for HMAC-SHA256 signing
    events = Column(JSONB, default=list)                   # ["report.ready", "lead.hot", ...]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    payload = Column(JSONB)
    status_code = Column(Integer)
    response_body = Column(Text)
    success = Column(Boolean, default=False)
    attempt = Column(Integer, default=1)
    delivered_at = Column(DateTime, default=datetime.utcnow)


# MAS TRM 13-domain controls
MAS_TRM_DOMAINS = [
    "Technology Risk Governance",
    "IT Project and Change Management",
    "Technology Operations",
    "IT Outsourcing and Vendor Management",
    "Cyber Security",
    "Data and Information Management",
    "Customer Awareness and Education",
    "Incident Management",
    "IT Audit",
    "Business Continuity and Disaster Recovery",
    "Technology Testing",
    "Cloud Computing",
    "Authentication and Access Management",
]


class TrmControl(Base):
    __tablename__ = "trm_controls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    domain = Column(String(100), nullable=False)           # one of MAS_TRM_DOMAINS
    control_ref = Column(String(50))                       # e.g. "TRM-5.2"
    description = Column(Text)
    status = Column(String(30), default="not_started")     # not_started | in_progress | compliant | gap
    gap_analysis = Column(Text)                            # AI-generated gap narrative
    risk_rating = Column(String(20))                       # low | medium | high | critical
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrmEvidence(Base):
    __tablename__ = "trm_evidence"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_id = Column(UUID(as_uuid=True), ForeignKey("trm_controls.id"), nullable=False)
    file_name = Column(String(255))
    s3_key = Column(Text)
    hash_value = Column(String(64))
    tx_hash = Column(String(66))                           # blockchain anchor
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    data_category = Column(String(100), nullable=False)    # e.g. "personal_data", "audit_logs"
    retention_days = Column(Integer, nullable=False)
    auto_purge = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SsoConfig(Base):
    __tablename__ = "sso_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), unique=True, nullable=False)
    protocol = Column(String(20), nullable=False)          # saml | oidc
    # SAML fields
    idp_metadata_url = Column(Text)
    idp_entity_id = Column(Text)
    sp_acs_url = Column(Text)
    # OIDC fields
    client_id = Column(String(255))
    client_secret = Column(String(255))
    discovery_url = Column(Text)
    # Common
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhiteLabelConfig(Base):
    __tablename__ = "white_label_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), unique=True, nullable=False)
    logo_s3_key = Column(Text)
    primary_color = Column(String(7), default="#10b981")   # hex
    secondary_color = Column(String(7), default="#0f172a")
    footer_text = Column(Text)
    report_header_text = Column(Text)
    custom_domain = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SlaLog(Base):
    __tablename__ = "sla_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    target_minutes = Column(Integer)
    actual_minutes = Column(Integer)
    met = Column(Boolean)
    event_metadata = Column("metadata", JSONB, default=dict)
    recorded_at = Column(DateTime, default=datetime.utcnow)


# ── Organisation invites (team collaboration) ────────────────────────────────
class OrganisationInvite(Base):
    __tablename__ = "organisation_invites"
    __table_args__ = (UniqueConstraint("organisation_id", "email", name="uq_org_invite_email"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    role = Column(String(50), default="member")            # admin | member
    token = Column(String(64), unique=True, nullable=False, index=True)
    invited_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), default="pending")         # pending | accepted | revoked | expired
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    accepted_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Shared vendor watchlist (team collaboration) ─────────────────────────────
class VendorWatchlistItem(Base):
    __tablename__ = "vendor_watchlist_items"
    __table_args__ = (UniqueConstraint("organisation_id", "vendor_ref", name="uq_watchlist_org_vendor"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True)
    # vendor_ref accepts either a marketplace vendor slug or a free-form identifier so
    # we don't need to FK directly to a single vendor table (multiple vendor models exist).
    vendor_ref = Column(String(255), nullable=False)
    vendor_name = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    added_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Per-vendor comments inside an org's watchlist ────────────────────────────
class VendorWatchlistComment(Base):
    __tablename__ = "vendor_watchlist_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    watchlist_item_id = Column(UUID(as_uuid=True), ForeignKey("vendor_watchlist_items.id", ondelete="CASCADE"), nullable=False, index=True)
    author_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ============================================================
# Extracted from models_gebiz.py
# ============================================================
"""
GeBIZ Tender Model
==================
Stores live GeBIZ open tenders fetched by the periodic sync task.
Distinct from TenderShortlist (win-probability catalogue) — this table
holds the real-time tender feed for the Opportunities page and ticker.
"""

import uuid
from datetime import datetime

from sqlalchemy import (Column, Date, DateTime, Float, Index, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.db import Base


class GebizTender(Base):
    __tablename__ = "gebiz_tenders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tender_no = Column(String(100), unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    agency = Column(String(255), nullable=False, index=True)
    closing_date = Column(DateTime, nullable=True, index=True)
    estimated_value = Column(Float, nullable=True)
    status = Column(String(50), nullable=False, default="Open", index=True)
    url = Column(Text, nullable=True)
    raw_data = Column(JSONB, nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_gebiz_tenders_status_closing", "status", "closing_date"),
    )


class GebizAwardHistory(Base):
    """Persisted row-level award history from data.gov.sg.

    `refresh_gebiz_base_rates` previously aggregated this into per-sector
    base rates and discarded the rows. The Tender Intelligence product
    needs the raw rows for historical award lookup and trend reporting.
    """

    __tablename__ = "gebiz_award_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_no = Column(String(100), nullable=True, index=True)
    awarded_date = Column(Date, nullable=True, index=True)
    supplier_name = Column(String(255), nullable=True, index=True)
    award_amt = Column(Numeric(14, 2), nullable=True)
    tender_description = Column(Text, nullable=True)
    procuring_entity = Column(String(255), nullable=True, index=True)
    sector = Column(String(100), nullable=True, index=True)
    raw = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tender_no", "supplier_name", "awarded_date",
            name="uq_gebiz_award_history_tender_supplier_date",
        ),
        Index("ix_gebiz_award_entity_date", "procuring_entity", "awarded_date"),
    )


# ============================================================
# Extracted from models_v10.py
# ============================================================
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

import enum
import uuid
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
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

    # Contact scraping
    contact_email = Column(String(255), nullable=True, index=True)
    scraped_data = Column(JSON, nullable=True)  # {emails: [], social_links: [], dpo_email: ...}
    last_scraped_at = Column(DateTime, nullable=True)

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

    # Contact scraping
    website = Column(String(500), nullable=True)
    contact_email = Column(String(255), nullable=True, index=True)
    scraped_data = Column(JSON, nullable=True)
    last_scraped_at = Column(DateTime, nullable=True)

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


# ── SearchImpression ──────────────────────────────────────────────────────────
# One row per appearance of a *claimed* vendor in a buyer search result. Powers
# the Vendor Active "your profile appeared in N searches this month" metric.
# Written best-effort by the marketplace / discovery search endpoints; a write
# failure there is swallowed and never blocks search.
class SearchImpression(Base):
    __tablename__ = "search_impressions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The claiming user (vendor) whose profile was shown. Not FK-constrained to
    # keep the hot search path cheap and resilient to id drift.
    vendor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    source = Column(String(20), nullable=False)  # "marketplace" | "discovery"
    query = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_search_impressions_vendor_created", "vendor_id", "created_at"),
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


# ── VendorTenderIntent ────────────────────────────────────────────────────────
# Per-vendor "I'm bidding / watching / passing" state for a live GeBIZ tender.
# Powers the in-app Tender Intelligence feed's action loop. Distinct from the
# global TenderShortlist above (which is a per-tender win-rate calibration row,
# not a per-vendor save list). Tender fields are snapshotted at save time so the
# tracked list still renders even after the tender drops out of the live feed.
class VendorTenderIntent(Base):
    __tablename__ = "vendor_tender_intents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tender_no = Column(String(100), nullable=False, index=True)

    # Snapshot of the tender at the moment of tracking.
    title = Column(Text, nullable=True)
    agency = Column(String(255), nullable=True)
    sector = Column(String(100), nullable=True)
    estimated_value = Column(Float, nullable=True)
    closing_date = Column(DateTime, nullable=True)
    url = Column(Text, nullable=True)

    intent = Column(String(20), nullable=False, default="watch")
    # ^ bid | watch | pass | not_bidding
    bid_label = Column(String(10), nullable=True)  # classifier label at save time
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("vendor_id", "tender_no", name="uq_vendor_tender_intent"),
        Index("ix_vendor_tender_intents_vendor", "vendor_id", "updated_at"),
    )


# ── VendorTenderAlertSent ─────────────────────────────────────────────────────
# Dedup ledger for the daily BID-tender alert email: one row per (vendor,
# tender) we've already alerted, so a tender that stays open for days is only
# emailed once. GeBIZ tenders carry no creation timestamp, so this ledger is how
# "new to this vendor" is determined.
class VendorTenderAlertSent(Base):
    __tablename__ = "vendor_tender_alerts_sent"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tender_no = Column(String(100), nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("vendor_id", "tender_no", name="uq_vendor_tender_alert_sent"),
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


# ============================================================
# Extracted from models_v11.py
# ============================================================
"""
Booppa V11 — Enterprise Modules
================================
ComplianceRequirement  — Singapore regulation → required evidence type mapping
ManagedVendor          — Enterprise buyer's monitored vendor portfolio
"""

import uuid
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
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


# ============================================================
# Extracted from models_v12.py
# ============================================================
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

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Index,
                        Integer, String, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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


# ============================================================
# Extracted from models_v13.py
# ============================================================
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

from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

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


import enum
# ============================================================
# Extracted from models_v6.py
# ============================================================
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import (Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
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
    company_name = Column(String(255), nullable=True)
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
    __table_args__ = (UniqueConstraint("vendor_id", "sector", name="uq_vendor_sector"),)

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


# ============================================================
# Extracted from models_v8.py
# ============================================================
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

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
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

    # On-chain verification (Buyer Enterprise). Populated asynchronously by
    # anchor_scan_ledger_task after the scan is consumed; NULL until anchored.
    tx_hash = Column(String(128), nullable=True, index=True)
    anchored_at = Column(DateTime, nullable=True)
    anchor_error = Column(Text, nullable=True)

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


# ============================================================
# Extracted from models_vendor_pro.py
# ============================================================
"""
Vendor Pro — supporting models.

TenderCheckLookup: append-only log of /tender-check calls. Powers the
competitor-awareness signal surfaced to Vendor Pro subscribers (counts
of verified vendors who probed a tender, no identities exposed).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.db import Base


class TenderCheckLookup(Base):
    __tablename__ = "tender_check_lookups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_no = Column(String(100), index=True, nullable=True)
    vendor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Denormalised sector to make sector-match queries cheap.
    sector = Column(String(100), nullable=True, index=True)
    # Was the looking-up vendor a verified vendor at the time of lookup?
    is_verified = Column(Boolean, default=False, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    __table_args__ = (
        Index("ix_tender_check_lookups_tender_created", "tender_no", "created_at"),
        Index("ix_tender_check_lookups_sector_created", "sector", "created_at"),
    )
