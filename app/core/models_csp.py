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

from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean,
    Enum, ForeignKey, DateTime, Index, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
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
