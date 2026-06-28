"""
Booppa CSP Compliance Pack — Pydantic schemas v3 (additions)
Layer 1: AML Programme approval attestation
Layer 2: Risk classification audit
Layer 3: ToS acceptance
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, field_validator, validator


class BooppaBase(BaseModel):
    class Config:
        from_attributes = True
        use_enum_values  = True
        json_encoders    = {datetime: lambda v: v.isoformat()}


# ── LAYER 1 — AML Programme Approval Attestation ──────────────────────────────

ATTESTATION_DECLARATIONS = [
    "I have personally verified that the content accurately reflects "
    "my CSP's operating procedures.",
    "I have consulted an AML/CFT legal expert, or I consider specialist "
    "legal advice unnecessary for this document.",
    "I remain solely responsible for my CSP's compliance with ACRA, the "
    "CSP Act 2024 and the CSP Regulations 2025. Approving this document "
    "does not transfer that responsibility to Booppa Smart Care LLC.",
]

ATTESTATION_TEXT = "\n".join(
    f"({i+1}) {d}" for i, d in enumerate(ATTESTATION_DECLARATIONS)
)


class ProgrammeApprovalAttestation(BooppaBase):
    """
    Mandatory payload to approve an AML/CFT Programme.
    All three declarations must be True — the server verifies them.
    """
    approved_by: str = Field(..., min_length=2, max_length=255,
                             description="Name and role of the compliance officer or managing director approving")

    declaration_content_accurate: bool = Field(
        ...,
        description="(1) The content accurately reflects the CSP's operating procedures"
    )
    declaration_legal_advice_considered: bool = Field(
        ...,
        description="(2) Legal advice has been considered or deemed unnecessary"
    )
    declaration_sole_responsible: bool = Field(
        ...,
        description="(3) The CSP remains solely responsible for its own regulatory compliance"
    )

    @field_validator("declaration_content_accurate", "declaration_legal_advice_considered",
                     "declaration_sole_responsible")
    @classmethod
    def must_be_true(cls, v, info):
        if not v:
            raise ValueError(
                f"The declaration '{info.field_name}' must be confirmed (true). "
                "All three declarations are mandatory to approve the document."
            )
        return v


class ProgrammeApprovalOut(BooppaBase):
    programme_id:    UUID
    status:          str
    approved_by:     str
    approved_at:     datetime
    attestation_id:  UUID
    next_review:     str
    notarized:       bool
    blockchain_tx:   Optional[str]
    polygonscan_url: Optional[str]
    legal_message:   str


# ── LAYER 2 — Risk Classification Audit ───────────────────────────────────────

class RiskClassificationCreate(BooppaBase):
    """
    Payload to update a client's risk_rating.
    Includes the mandatory rationale fields + a snapshot of the flags.
    """
    risk_rating:    str = Field(..., description="low | medium | high | very_high")
    risk_rationale: str = Field(..., min_length=20, max_length=2000,
                                description="Explicit rationale for the classification (min 20 characters)")
    classified_by:  str = Field(..., min_length=2, max_length=255,
                                description="Name of the person responsible for the classification")
    additional_risk_flags: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional additional flags: {unusual_transactions: true, complex_structure: true, ...}"
    )

    @validator("risk_rating")
    def validate_rating(cls, v):
        allowed = {"low", "medium", "high", "very_high"}
        if v not in allowed:
            raise ValueError(f"risk_rating must be one of: {allowed}")
        return v

    @validator("risk_rationale")
    def validate_rationale(cls, v):
        if len(v.strip()) < 20:
            raise ValueError(
                "risk_rationale must contain at least 20 characters. "
                "The rationale for the risk classification is mandatory "
                "and must be documented under the CSP Act 2024."
            )
        return v


class RiskClassificationOut(BooppaBase):
    client_id:           UUID
    audit_id:            UUID
    risk_rating_assigned: str
    risk_rating_previous: Optional[str]
    classified_by:        str
    classified_at:        datetime
    notarized:            bool
    blockchain_tx:        Optional[str]
    polygonscan_url:      Optional[str]
    legal_note:           str


# ── LAYER 3 — ToS Acceptance ──────────────────────────────────────────────────

TOS_VERSION_CURRENT = "1.0"

TOS_CLAUSES = {
    "ai_disclaimer": (
        "CLAUSE 1 — AI-Generated Content Disclaimer: The documents produced by the system "
        "(AML/CFT Programme, CDD Procedures, STR Policy and all others) are generated by an "
        "AI system based on the information provided by the CSP. Booppa does not guarantee "
        "that these documents satisfy the specific ACRA or CDSA regulatory requirements "
        "applicable to the CSP. The CSP remains solely responsible for its own regulatory "
        "compliance. Use of these documents is not a substitute for specialist AML/CFT "
        "legal advice."
    ),
    "data_accuracy": (
        "CLAUSE 2 — Data Accuracy Obligation: The accuracy of the system's outputs depends "
        "entirely on the accuracy and completeness of the information provided by the CSP. "
        "Booppa does not independently verify the information entered by the CSP."
    ),
    "sanctions_limitation": (
        "CLAUSE 3 — Sanctions Screening Limitation: The system provides integration with the "
        "OFAC SDN, UN Consolidated and EU Consolidated sanctions lists as an operational aid. "
        "The CSP remains responsible for conducting independent sanctions screening and for "
        "verifying the completeness and currency of the lists checked."
    ),
    "regulatory_change": (
        "CLAUSE 4 — Regulatory Change Risk: The regulations applicable to Singapore CSPs may "
        "change. Booppa undertakes to update the system within reason, but does not guarantee "
        "that the templates and workflows reflect the latest regulatory changes at all times. "
        "The CSP is responsible for verifying the current compliance of its own processes."
    ),
    "liability_cap": (
        "CLAUSE 5 — Limitation of Liability: In no event shall the aggregate liability of "
        "Booppa Smart Care LLC to the CSP exceed the total fees paid in the 12 months "
        "preceding the event giving rise to the claim. For a CSP on the S$299/month plan, "
        "this corresponds to a maximum cap of S$3,588. Consequential, indirect, incidental "
        "and punitive damages are excluded in all cases."
    ),
}


class TosAcceptanceCreate(BooppaBase):
    """
    The CSP must confirm each clause individually.
    All checkboxes must be True — verified server-side.
    """
    tos_version: str = Field(TOS_VERSION_CURRENT)

    checkbox_ai_disclaimer:        bool
    checkbox_data_accuracy:        bool
    checkbox_sanctions_limitation: bool
    checkbox_regulatory_change:    bool
    checkbox_liability_cap:        bool

    # Optional audit metadata (the server can also read these from the request headers)
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    @field_validator(
        "checkbox_ai_disclaimer", "checkbox_data_accuracy",
        "checkbox_sanctions_limitation", "checkbox_regulatory_change",
        "checkbox_liability_cap",
    )
    @classmethod
    def all_must_be_true(cls, v, info):
        if not v:
            raise ValueError(
                f"The checkbox '{info.field_name}' must be confirmed. "
                "All five clauses must be accepted."
            )
        return v


class TosAcceptanceOut(BooppaBase):
    acceptance_id:     UUID
    csp_id:            UUID
    tos_version:       str
    accepted_at:       datetime
    liability_cap_sgd: float
    notarized:         bool
    blockchain_tx:     Optional[str]
    polygonscan_url:   Optional[str]
    message:           str
