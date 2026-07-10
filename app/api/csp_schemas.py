from __future__ import annotations
"""
Booppa CSP Compliance Pack — Pydantic Schemas
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, validator


class BooppaBase(BaseModel):
    class Config:
        from_attributes = True
        use_enum_values  = True
        json_encoders    = {datetime: lambda v: v.isoformat()}


# ── PROFILE ────────────────────────────────────────────────────────────────

class CspProfileCreate(BooppaBase):
    legal_name:          str = Field(..., min_length=2, max_length=255)
    uen:                 str = Field(..., min_length=9, max_length=20)
    registered_address:  Optional[str] = None
    business_email:      Optional[EmailStr] = None
    business_phone:      Optional[str] = None
    rqi_name:            Optional[str] = None
    rqi_qualification:   Optional[str] = None
    aml_compliance_officer: Optional[str] = None
    offers_company_formation:    bool = False
    offers_nominee_director:     bool = False
    offers_nominee_shareholder:  bool = False
    offers_registered_address:   bool = False
    offers_corp_secretarial:     bool = False
    offers_shelf_company:        bool = False


class CspProfileOut(BooppaBase):
    id:                      UUID
    legal_name:              str
    uen:                     str
    acra_reg_status:         str
    acra_reg_number:         Optional[str]
    acra_renewal_date:       Optional[datetime]
    rqi_name:                Optional[str]
    rqi_qualification:       Optional[str]
    rqi_training_completed:  bool
    aml_compliance_officer:  Optional[str]
    aml_programme_exists:    bool
    overall_compliance_score: float
    last_scored_at:          Optional[datetime]
    csp_pack_tier:           str
    created_at:              datetime
    services: List[str] = []

    @classmethod
    def from_orm_with_services(cls, obj):
        services = [
            k.replace("offers_", "").replace("_", " ").title()
            for k in ["offers_company_formation","offers_nominee_director",
                      "offers_nominee_shareholder","offers_registered_address",
                      "offers_corp_secretarial","offers_shelf_company"]
            if getattr(obj, k, False)
        ]
        d = {c.key: getattr(obj, c.key) for c in obj.__table__.columns}
        d["services"] = services
        return cls(**{k: v for k, v in d.items() if k in cls.model_fields})


# ── CLIENT ─────────────────────────────────────────────────────────────────

class CspClientCreate(BooppaBase):
    client_type:       str = Field(..., description="individual | company | llp | foreign_co")
    legal_name:        str = Field(..., min_length=2, max_length=255)
    uen_or_reg_no:     Optional[str] = None
    country_of_inc:    Optional[str] = None
    registered_address: Optional[str] = None
    contact_name:      Optional[str] = None
    contact_email:     Optional[EmailStr] = None
    contact_phone:     Optional[str] = None
    services_provided: List[str] = []
    is_remote_onboarding: bool = False
    has_nominee_director:    bool = False
    has_nominee_shareholder: bool = False

    @validator("client_type")
    def validate_type(cls, v):
        allowed = {"individual","company","llp","foreign_co"}
        if v not in allowed:
            raise ValueError(f"client_type must be one of {allowed}")
        return v


class CspClientOut(BooppaBase):
    id:                UUID
    client_type:       str
    legal_name:        str
    uen_or_reg_no:     Optional[str]
    country_of_inc:    Optional[str]
    contact_name:      Optional[str]
    contact_email:     Optional[str]
    risk_rating:       str
    cdd_status:        str
    cdd_completed_at:  Optional[datetime]
    cdd_next_review:   Optional[datetime]
    edd_required:      bool
    is_pep:            bool
    high_risk_country: bool
    is_remote_onboarding: bool
    video_call_completed: bool
    has_nominee_director:    bool
    has_nominee_shareholder: bool
    str_filed:         bool
    str_count:         int
    is_active:         bool
    onboarded_at:      Optional[datetime]
    created_at:        datetime


class CspClientUpdate(BooppaBase):
    risk_rating:    Optional[str] = None
    cdd_status:     Optional[str] = None
    edd_required:   Optional[bool] = None
    is_active:      Optional[bool] = None
    risk_rationale: Optional[str] = None


# ── CDD ────────────────────────────────────────────────────────────────────

class CddCreate(BooppaBase):
    review_type:          str = Field("initial", description="initial | periodic | triggered | offboarding")
    # Individual
    individual_full_name:       Optional[str] = None
    individual_nric_or_passport: Optional[str] = None
    individual_dob:             Optional[str] = None
    individual_nationality:     Optional[str] = None
    id_doc_type:                Optional[str] = None
    id_doc_verified:            bool = False
    id_doc_expiry:              Optional[str] = None
    id_verification_method:     Optional[str] = None
    # Corporate
    corp_registration_verified:     bool = False
    corp_constitution_obtained:     bool = False
    corp_directors_identified:      bool = False
    corp_shareholders_identified:   bool = False
    # Purpose
    business_purpose:   Optional[str] = None
    source_of_funds:    Optional[str] = None
    source_of_wealth:   Optional[str] = None
    # Remote
    non_face_to_face:         bool = False
    video_call_completed:     bool = False
    video_call_recording_ref: Optional[str] = None
    # Screening
    sanctions_screened:        bool = False
    sanctions_clear:           Optional[bool] = None
    sanctions_screen_provider: Optional[str] = None
    pep_screening_done:        bool = False
    pep_result:                Optional[str] = None
    adverse_media_checked:     bool = False
    # Completion
    completed_by:      Optional[str] = None
    next_review_date:  Optional[datetime] = None
    failure_reason:    Optional[str] = None
    evidence_files:    List[Dict] = []


class CddOut(BooppaBase):
    id:                UUID
    client_id:         UUID
    review_type:       str
    status:            str
    completed_by:      Optional[str]
    completed_at:      Optional[datetime]
    next_review_date:  Optional[datetime]
    failure_reason:    Optional[str]
    sanctions_screened: bool
    sanctions_clear:   Optional[bool]
    pep_screening_done: bool
    pep_result:        Optional[str]
    non_face_to_face:  bool
    video_call_completed: bool
    blockchain_tx_hash: Optional[str]
    polygonscan_url:   Optional[str]
    created_at:        datetime


# ── STR ────────────────────────────────────────────────────────────────────

class StrCreate(BooppaBase):
    client_id:       Optional[UUID] = None
    trigger_type:    str
    trigger_detail:  str = Field(..., min_length=20)
    amount_involved: Optional[float] = None
    currency:        Optional[str] = "SGD"
    transaction_date: Optional[datetime] = None
    decision:        str = Field(..., description="filed | not_filed | pending | escalated")
    decision_by:     str
    decision_rationale: str = Field(..., min_length=20,
                                    description="Mandatory even if not filing")
    stro_reference:  Optional[str] = None
    stro_filed_date: Optional[datetime] = None
    service_declined: bool = False

    @validator("decision")
    def validate_decision(cls, v):
        if v not in ("filed","not_filed","pending","escalated"):
            raise ValueError("decision must be: filed | not_filed | pending | escalated")
        return v


class StrOut(BooppaBase):
    id:                UUID
    client_id:         Optional[UUID]
    trigger_type:      str
    trigger_detail:    str
    decision:          str
    decision_by:       str
    decision_date:     Optional[datetime]
    decision_rationale: str
    stro_reference:    Optional[str]
    stro_filed_date:   Optional[datetime]
    service_declined:  bool
    client_notified:   bool
    blockchain_tx_hash: Optional[str]
    polygonscan_url:   Optional[str]
    created_at:        datetime


# ── NOMINEE ────────────────────────────────────────────────────────────────

class NomineeDirectorCreate(BooppaBase):
    client_id:           UUID
    nominee_full_name:   str
    nominee_nationality: Optional[str] = None
    nominator_name:      str
    company_name:        Optional[str] = None
    company_uen:         Optional[str] = None
    appointment_date:    Optional[datetime] = None


class NomineeDirectorOut(BooppaBase):
    id:                UUID
    client_id:         UUID
    nominee_full_name: str
    nominator_name:    str
    company_name:      Optional[str]
    company_uen:       Optional[str]
    assessment_status: str
    assessment_date:   Optional[datetime]
    acra_disclosed:    bool
    acra_filing_date:  Optional[datetime]
    is_active:         bool
    next_review:       Optional[datetime]
    blockchain_tx_hash: Optional[str]
    created_at:        datetime


class NomineeAssessmentUpdate(BooppaBase):
    assessment_outcome:    str
    assessed_by:           str
    criminal_check_done:   bool
    bankruptcy_check_done: bool
    director_history_check: bool
    assessment_notes:      Optional[str] = None
    result:                str = Field(..., description="fit_proper | not_fit | under_review")


# ── BENEFICIAL OWNER ───────────────────────────────────────────────────────

class UboCreate(BooppaBase):
    client_id:              UUID
    ubo_full_name:          str
    ubo_nationality:        Optional[str] = None
    ubo_dob:                Optional[str] = None
    ubo_address:            Optional[str] = None
    ubo_country_of_residence: Optional[str] = None
    ownership_percentage:   Optional[float] = None
    control_mechanism:      Optional[str] = None
    is_pep:                 bool = False
    identity_verified:      bool = False
    verification_method:    Optional[str] = None
    verification_date:      Optional[datetime] = None
    verified_by:            Optional[str] = None


class UboOut(BooppaBase):
    id:                    UUID
    client_id:             UUID
    ubo_full_name:         str
    ubo_nationality:       Optional[str]
    ownership_percentage:  Optional[float]
    control_mechanism:     Optional[str]
    is_pep:                bool
    is_sanctioned:         bool
    identity_verified:     bool
    verification_date:     Optional[datetime]
    next_review:           Optional[datetime]
    blockchain_tx_hash:    Optional[str]
    created_at:            datetime


# ── COMPLIANCE SCORE ───────────────────────────────────────────────────────

class PillarScore(BooppaBase):
    pillar:   str
    score:    float
    status:   str
    gaps:     List[str]
    urgent:   List[str]
    weight:   float
    stats:    Optional[Dict[str, Any]] = None


class ComplianceScoreOut(BooppaBase):
    overall_score:    float
    risk_level:       str
    pillars:          Dict[str, PillarScore]
    urgent_actions:   List[Dict[str, str]]
    all_gaps:         List[Dict[str, str]]
    critical_pillars: List[str]
    computed_at:      str


# ── TRAINING ───────────────────────────────────────────────────────────────

class TrainingCreate(BooppaBase):
    staff_name:     str
    staff_role:     str
    is_rqi:         bool = False
    training_type:  str
    training_title: str
    provider:       str
    training_date:  Optional[datetime] = None
    completion_date: Optional[datetime] = None
    expiry_date:    Optional[datetime] = None
    score:          Optional[int] = None
    certificate_ref: Optional[str] = None


class TrainingOut(BooppaBase):
    id:              UUID
    staff_name:      str
    staff_role:      str
    is_rqi:          bool
    training_type:   str
    training_title:  str
    provider:        str
    completion_date: Optional[datetime]
    expiry_date:     Optional[datetime]
    status:          str
    score:           Optional[int]
    blockchain_tx_hash: Optional[str]
    created_at:      datetime


# ── CALENDAR ───────────────────────────────────────────────────────────────

class CalendarItemOut(BooppaBase):
    id:               UUID
    pillar:           str
    title:            str
    description:      Optional[str]
    due_date:         datetime
    frequency:        Optional[str]
    legal_basis:      Optional[str]
    penalty_if_missed: Optional[str]
    status:           str
    completed_at:     Optional[datetime]
    days_remaining:   Optional[int]


# ── DASHBOARD ──────────────────────────────────────────────────────────────

class CspDashboardOut(BooppaBase):
    profile:                CspProfileOut
    compliance_score:       ComplianceScoreOut
    client_stats: Dict = {}
    upcoming_deadlines:     List[CalendarItemOut] = []
    overdue_items:          List[CalendarItemOut] = []
    recent_cdd_activity:    List[CddOut] = []
    open_str_decisions:     List[StrOut] = []
    nominees_pending_review: List[NomineeDirectorOut] = []
    ubos_pending_update:    List[UboOut] = []


# ── PRICING ────────────────────────────────────────────────────────────────

class CspPackPricing(BooppaBase):
    tier:            str
    name:            str
    price_one_time:  float
    price_monthly:   float
    documents:       int
    features:        List[str]
    recommended:     bool


CSP_PACK_CATALOG: List[CspPackPricing] = [
    CspPackPricing(
        tier="full",
        name="CSP Compliance Pack — Full",
        price_one_time=3999.0,
        price_monthly=299.0,
        documents=8,
        features=[
            "Complete AML/CFT/PF Programme (8 documents) generated by AI, notarized on blockchain",
            "Client registry with CDD tracker for unlimited clients",
            "EDD workflow for PEPs and high-risk clients",
            "STR decision framework with documented rationale logging",
            "Nominee Director register + fit-and-proper assessment workflow",
            "Nominee Shareholder register + ACRA disclosure tracker",
            "Beneficial Owner (UBO) identification and verification tracker",
            "Risk-Based Approach scoring per client (automated composite score)",
            "Regulatory Compliance Calendar — all deadlines with automated alerts",
            "Staff AML/CFT training records (RQI + all staff)",
            "Blockchain notarization of every CDD, EDD, STR decision, and training record",
            "Monthly ACRA/PDPC/FATF monitoring alerts",
            "PDPA + NRIC compliance included (full Remediation Engine access)",
            "Completion Certificate for ACRA licence renewal evidence package",
        ],
        recommended=True,
    ),
    CspPackPricing(
        tier="monitoring_only",
        name="CSP Monitoring Add-On",
        price_one_time=0.0,
        price_monthly=299.0,
        documents=0,
        features=[
            "Continuous monitoring of ACRA enforcement decisions",
            "FATF grey/black list updates",
            "PDPC enforcement alerts (relevant to CSP data handling)",
            "Sanctions list updates — OFAC, UN & EU (MAS via World-Check)",
            "Regulatory deadline reminders with escalation",
            "Monthly compliance health report",
        ],
        recommended=False,
    ),
]
