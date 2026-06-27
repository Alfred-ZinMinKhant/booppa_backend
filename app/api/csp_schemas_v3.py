"""
Booppa CSP Compliance Pack — Schemi Pydantic v3 (aggiunte)
Intervento 1: AML Programme approval attestation
Intervento 2: Risk classification audit
Intervento 3: ToS acceptance
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


# ── INTERVENTO 1 — AML Programme Approval Attestation ─────────────────────────

ATTESTATION_DECLARATIONS = [
    "Ho verificato personalmente che il contenuto riflette accuratamente "
    "le procedure operative del mio CSP.",
    "Ho consultato un esperto legale AML/CFT oppure ritengo non necessaria "
    "la consulenza legale specializzata per questo documento.",
    "Rimango l'unico responsabile della conformità del mio CSP alle normative "
    "ACRA, CSP Act 2024 e CSP Regulations 2025. L'approvazione di questo documento "
    "non trasferisce tale responsabilità a Booppa Smart Care LLC.",
]

ATTESTATION_TEXT = "\n".join(
    f"({i+1}) {d}" for i, d in enumerate(ATTESTATION_DECLARATIONS)
)


class ProgrammeApprovalAttestation(BooppaBase):
    """
    Payload obbligatorio per approvare un AML/CFT Programme.
    Tutte e tre le dichiarazioni devono essere True — il server le verifica.
    """
    approved_by: str = Field(..., min_length=2, max_length=255,
                             description="Nome e ruolo del compliance officer o managing director che approva")

    declaration_content_accurate: bool = Field(
        ...,
        description="(1) Il contenuto riflette accuratamente le procedure operative del CSP"
    )
    declaration_legal_advice_considered: bool = Field(
        ...,
        description="(2) Consulenza legale è stata valutata o ritenuta non necessaria"
    )
    declaration_sole_responsible: bool = Field(
        ...,
        description="(3) Il CSP rimane l'unico responsabile della propria conformità normativa"
    )

    @field_validator("declaration_content_accurate", "declaration_legal_advice_considered",
                     "declaration_sole_responsible")
    @classmethod
    def must_be_true(cls, v, info):
        if not v:
            raise ValueError(
                f"La dichiarazione '{info.field_name}' deve essere confermata (true). "
                "Tutte e tre le dichiarazioni sono obbligatorie per approvare il documento."
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


# ── INTERVENTO 2 — Risk Classification Audit ──────────────────────────────────

class RiskClassificationCreate(BooppaBase):
    """
    Payload per aggiornare il risk_rating di un cliente.
    Include i campi obbligatori di motivazione + snapshot dei flag.
    """
    risk_rating:    str = Field(..., description="low | medium | high | very_high")
    risk_rationale: str = Field(..., min_length=20, max_length=2000,
                                description="Motivazione esplicita della classificazione (min 20 caratteri)")
    classified_by:  str = Field(..., min_length=2, max_length=255,
                                description="Nome del responsabile della classificazione")
    additional_risk_flags: Optional[Dict[str, Any]] = Field(
        None,
        description="Flag aggiuntivi opzionali: {unusual_transactions: true, complex_structure: true, ...}"
    )

    @validator("risk_rating")
    def validate_rating(cls, v):
        allowed = {"low", "medium", "high", "very_high"}
        if v not in allowed:
            raise ValueError(f"risk_rating deve essere uno di: {allowed}")
        return v

    @validator("risk_rationale")
    def validate_rationale(cls, v):
        if len(v.strip()) < 20:
            raise ValueError(
                "risk_rationale deve contenere almeno 20 caratteri. "
                "La motivazione della classificazione di rischio è obbligatoria "
                "e deve essere documentata ai sensi del CSP Act 2024."
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


# ── INTERVENTO 3 — ToS Acceptance ─────────────────────────────────────────────

TOS_VERSION_CURRENT = "1.0"

TOS_CLAUSES = {
    "ai_disclaimer": (
        "CLAUSOLA 1 — AI-Generated Content Disclaimer: I documenti generati dal sistema "
        "(AML/CFT Programme, CDD Procedures, STR Policy e tutti gli altri) sono prodotti "
        "da un sistema AI basato sulle informazioni fornite dal CSP. Booppa non garantisce "
        "che questi documenti soddisfino i requisiti normativi specifici dell'ACRA o del CDSA "
        "applicabili al CSP. Il CSP rimane l'unico responsabile della propria compliance "
        "normativa. L'uso di questi documenti non sostituisce la consulenza legale "
        "specializzata in AML/CFT."
    ),
    "data_accuracy": (
        "CLAUSOLA 2 — Data Accuracy Obligation: L'accuratezza dei risultati del sistema "
        "dipende interamente dall'accuratezza e completezza delle informazioni fornite dal CSP. "
        "Booppa non verifica in modo indipendente le informazioni inserite dal CSP."
    ),
    "sanctions_limitation": (
        "CLAUSOLA 3 — Sanctions Screening Limitation: Il sistema fornisce integrazione "
        "con OFAC SDN List e UN Consolidated Sanctions List come ausilio operativo. "
        "Il CSP rimane responsabile di condurre sanctions screening indipendente e di "
        "verificare la completezza e aggiornamento delle liste controllate."
    ),
    "regulatory_change": (
        "CLAUSOLA 4 — Regulatory Change Risk: La normativa applicabile ai CSP SG può "
        "cambiare. Booppa si impegna ad aggiornare il sistema nei limiti del ragionevole, "
        "ma non garantisce che i template e i workflow riflettano in ogni momento le ultime "
        "modifiche normative. Il CSP è responsabile di verificare la conformità attuale "
        "dei propri processi."
    ),
    "liability_cap": (
        "CLAUSOLA 5 — Limitation of Liability: In nessun caso la responsabilità aggregata "
        "di Booppa Smart Care LLC verso il CSP supera il totale delle fees pagate nei 12 mesi "
        "precedenti all'evento che ha dato origine alla claim. Per un CSP con piano mensile "
        "S$299/mese, questo corrisponde a un cap massimo di S$3.588. Danni consequenziali, "
        "indiretti, incidentali e punitivi sono esclusi in ogni caso."
    ),
}


class TosAcceptanceCreate(BooppaBase):
    """
    Il CSP deve confermare ogni clausola individualmente.
    Tutti i checkbox devono essere True — verificato server-side.
    """
    tos_version: str = Field(TOS_VERSION_CURRENT)

    checkbox_ai_disclaimer:        bool
    checkbox_data_accuracy:        bool
    checkbox_sanctions_limitation: bool
    checkbox_regulatory_change:    bool
    checkbox_liability_cap:        bool

    # Metadati facoltativi per audit (il server li può anche leggere dall'header)
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
                f"Il checkbox '{info.field_name}' deve essere confermato. "
                "Tutte e cinque le clausole devono essere accettate."
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
