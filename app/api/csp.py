"""
Booppa CSP Compliance Pack — FastAPI Router (v3)

Fixes rispetto a v2:
  FIX AUTH:    get_current_user() stub rimosso — sostituito con vera auth JWT
  FIX ROUTE:   route conflict /clients/bulk-import/template risolto (prefix statico prima)
  FIX DICT:    _to_dict() ora esclude campi cifrati dal compliance scorer
  FIX DASHBOARD: 9 query sequenziali → query ottimizzate con selectinload
  FIX SANCTIONS: screening asincrono via Celery — non più inline nel thread HTTP
  FIX DATES:   replace(year=+1) → relativedelta(years=1) — sicuro su anni bisestili

Interventi v3 (da Sicurezza_e_rischi_legali.docx):
  INTERVENTO 1: Approval attestation non bypassabile per AML/CFT Programme
  INTERVENTO 2: Risk classification notarizzata su Polygon (customer input audit)
  INTERVENTO 3: ToS acceptance endpoint con liability cap esplicito + blockchain proof

Mounted via app/api/__init__.py (the composite api_router is dual-mounted at /api and
/api/v1 in app/main.py). This router self-prefixes "/csp", so endpoints land at both
/api/v1/csp/... and /api/csp/... — do NOT add a separate include_router call.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Query,
    UploadFile, File, Form, Request, status,
)
from fastapi.responses import Response

from app.core.models_csp import (
    CspProfile, CspClient, CspCddRecord, CspEddRecord, CspStrReport,
    CspNomineeDirector, CspNomineeShareholder, CspBeneficialOwner,
    CspAmlProgramme, CspRiskAssessment, CspComplianceCalendar,
    CspStaffTraining, CspBlockchainEvidence,
    CspTosAcceptance, CspProgrammeAttestation, CspRiskClassificationAudit,
    RegistrationStatus, CddStatus, StrDecision, NomineeAssessment, TrainingStatus,
    RiskRating,
)
from app.core.models_csp import CspOrganisation, CspOrgMembership
from app.api.csp_schemas import (
    CspProfileCreate, CspClientCreate, CspClientUpdate,
    CddCreate, StrCreate, NomineeDirectorCreate, NomineeAssessmentUpdate,
    UboCreate, TrainingCreate, CSP_PACK_CATALOG,
)
from app.api.csp_schemas_v3 import (
    ProgrammeApprovalAttestation, RiskClassificationCreate,
    TosAcceptanceCreate, TOS_CLAUSES, TOS_VERSION_CURRENT,
    ATTESTATION_TEXT,
)
from app.core.config import settings
from app.core.db import get_db, get_current_user as _booppa_get_current_user
from app.services.csp_access import find_or_create_csp_org
from app.services.csp_compliance_scorer import compute_overall_compliance
from app.services.csp_sanctions import screen_individual, screen_entity

try:
    from dateutil.relativedelta import relativedelta
    _HAS_RELATIVEDELTA = True
except ImportError:
    _HAS_RELATIVEDELTA = False


router = APIRouter(prefix="/csp")


# ── AUTH ADAPTER ────────────────────────────────────────────────────────────────
# The ported router was written for a JWT carrying an `org_id` claim. Booppa's auth
# returns a `User` model instead, so this adapter resolves (and auto-provisions on
# first use) the caller's CSP organisation and exposes the dict the router expects:
#   {"id", "org_id", "email", "roles", "monthly_fee_sgd"}

def _resolve_or_provision_org(db, user) -> CspOrganisation:
    # Org creation lives in the shared service so the Stripe webhook provisions
    # rows the same way the router does. This only ensures the row exists (and
    # starts inactive); access is granted by the webhook via activate_csp_access.
    return find_or_create_csp_org(db, user)


async def get_current_user(
    user=Depends(_booppa_get_current_user),
    db=Depends(get_db),
) -> dict:
    org = _resolve_or_provision_org(db, user)
    # Access gate: CSP is a paid pack. Until a Stripe purchase activates the org,
    # every authenticated endpoint is blocked. (GET /csp/pricing has no auth dep
    # and stays open so prospects can see the catalog.)
    if (org.subscription_status or "inactive") != "active":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="CSP subscription required. Purchase the CSP Compliance Pack to access these features.",
        )
    roles = [
        m.role
        for m in db.query(CspOrgMembership).filter(
            CspOrgMembership.org_id == org.id,
            CspOrgMembership.user_id == user.id,
        ).all()
    ]
    return {
        "id":     str(user.id),
        "org_id": str(org.id),
        "email":  user.email or "",
        "roles":  roles or ["csp_admin"],
        "monthly_fee_sgd": org.monthly_fee_sgd or 299.0,
    }


def require_role(required_role: str):
    """Dependency factory mirroring the pack's role gate."""
    def _check(current_user: dict = Depends(get_current_user)) -> dict:
        if required_role not in current_user.get("roles", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Ruolo richiesto: {required_role}",
            )
        return current_user
    return _check


# ── DATE HELPER — FIX leap year ────────────────────────────────────────────────

def _add_one_year(dt: datetime) -> datetime:
    """Aggiunge 1 anno a una datetime in modo sicuro (evita crash su 29 feb)."""
    if _HAS_RELATIVEDELTA:
        return dt + relativedelta(years=1)
    # Fallback sicuro senza dateutil
    try:
        return dt.replace(year=dt.year + 1)
    except ValueError:
        # 29 febbraio → 28 febbraio dell'anno successivo
        return dt.replace(year=dt.year + 1, day=28)


# ── PRICING ────────────────────────────────────────────────────────────────────

@router.get("/pricing", summary="CSP Compliance Pack pricing catalog")
def get_pricing():
    return CSP_PACK_CATALOG


# ── TOS ACCEPTANCE — INTERVENTO 3 ─────────────────────────────────────────────

@router.get("/tos", summary="Recupera testo completo dei Terms of Service con liability cap")
def get_tos(current_user: dict = Depends(get_current_user)):
    """
    Ritorna le 5 clausole specifiche per AI-generated compliance documents.
    Il frontend deve mostrarle con checkbox individuali prima di permettere
    la creazione del profilo o l'accesso al pack.
    """
    return {
        "version": TOS_VERSION_CURRENT,
        "clauses": TOS_CLAUSES,
        "liability_cap_explanation": (
            "La liability di Booppa è limitata a 12 mesi di fees pagate. "
            "Per piano S$299/mese: cap massimo S$3.588. "
            "Questo importo deve essere confermato esplicitamente al momento dell'accettazione."
        ),
        "instruction": (
            "Il CSP deve accettare tutte e cinque le clausole tramite "
            "POST /api/v1/csp/tos/accept prima di poter creare il profilo CSP."
        ),
    }


@router.post(
    "/tos/accept",
    status_code=status.HTTP_201_CREATED,
    summary="Accettazione formale ToS — liability cap esplicito + blockchain notarization",
)
def accept_tos(
    payload:      TosAcceptanceCreate,
    request:      Request,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    """
    Registra l'accettazione dei ToS con firma digitale.
    Notarizzata su Polygon — prova blockchain della consapevolezza del CSP.

    Tutte e 5 le clausole devono essere True (verificato da Pydantic).
    Un solo record per versione di ToS per CSP (UniqueConstraint).
    """
    # ToS is keyed by organisation (csp_id -> csp_organisations.id) because it is
    # accepted before a CspProfile exists. Always use org_id for consistency.
    profile = _get_profile_optional(db, current_user)
    csp_id_for_tos = uuid.UUID(current_user["org_id"])

    # Controlla se già accettato per questa versione
    existing = db.query(CspTosAcceptance).filter(
        CspTosAcceptance.csp_id == csp_id_for_tos,
        CspTosAcceptance.tos_version == payload.tos_version,
    ).first()
    if existing:
        return {
            "acceptance_id":    str(existing.id),
            "csp_id":           str(csp_id_for_tos),
            "tos_version":      existing.tos_version,
            "accepted_at":      existing.accepted_at.isoformat(),
            "liability_cap_sgd": existing.liability_cap_amount_sgd,
            "notarized":        bool(existing.blockchain_tx_hash),
            "blockchain_tx":    existing.blockchain_tx_hash,
            "polygonscan_url":  existing.polygonscan_url,
            "message":          "ToS già accettati per questa versione. Record esistente ritornato.",
        }

    # Calcola il liability cap in base al piano (default S$299/mese × 12)
    monthly_fee     = (profile.monthly_fee_sgd if profile
                       else current_user.get("monthly_fee_sgd", 299.0))
    liability_cap   = round(monthly_fee * 12, 2)
    liability_text  = (
        f"Confermo di aver letto e accettato i Termini di Servizio, "
        f"inclusa la limitazione di responsabilità a S${liability_cap:.2f} "
        f"(12 mesi di fees pagate a S${monthly_fee:.2f}/mese). "
        f"Il CSP rimane l'unico responsabile della propria conformità normativa."
    )

    now = datetime.now(timezone.utc)
    ip  = payload.ip_address or request.client.host if request.client else None
    ua  = payload.user_agent or request.headers.get("user-agent", "")

    acceptance = CspTosAcceptance(
        csp_id=csp_id_for_tos,
        user_id=uuid.UUID(current_user["id"]),
        user_email=current_user.get("email", ""),
        tos_version=payload.tos_version,
        accepted_at=now,
        ip_address=ip,
        user_agent=ua[:500] if ua else None,
        checkbox_ai_disclaimer=payload.checkbox_ai_disclaimer,
        checkbox_data_accuracy=payload.checkbox_data_accuracy,
        checkbox_sanctions_limitation=payload.checkbox_sanctions_limitation,
        checkbox_regulatory_change=payload.checkbox_regulatory_change,
        checkbox_liability_cap=payload.checkbox_liability_cap,
        liability_cap_amount_sgd=liability_cap,
        liability_cap_text_shown=liability_text,
    )

    # Hash deterministico del contenuto
    hash_content = {
        "csp_id":     str(csp_id_for_tos),
        "user_id":    current_user["id"],
        "email":      current_user.get("email", ""),
        "tos_version": payload.tos_version,
        "accepted_at": now.isoformat(),
        "all_checkboxes": True,
        "liability_cap_sgd": liability_cap,
    }
    acceptance.content_hash = hashlib.sha256(
        json.dumps(hash_content, sort_keys=True).encode()
    ).hexdigest()

    db.add(acceptance)
    db.commit()
    db.refresh(acceptance)

    # Notarizzazione blockchain asincrona
    from app.workers.csp_tasks import notarize_csp_record
    notarize_csp_record.apply_async(
        args=[str(acceptance.id), "tos_acceptance", str(csp_id_for_tos)], countdown=3
    )

    return {
        "acceptance_id":     str(acceptance.id),
        "csp_id":            str(csp_id_for_tos),
        "tos_version":       payload.tos_version,
        "accepted_at":       now.isoformat(),
        "liability_cap_sgd": liability_cap,
        "notarized":         True,
        "blockchain_tx":     None,  # disponibile dopo task async
        "polygonscan_url":   None,
        "message": (
            "ToS accettati formalmente. Notarizzazione blockchain in corso. "
            "Il record di accettazione costituisce prova della consapevolezza "
            "del CSP riguardo ai termini e al liability cap."
        ),
    }


# ── PROFILE ────────────────────────────────────────────────────────────────────

@router.post("/profile", status_code=status.HTTP_201_CREATED,
             summary="Crea profilo CSP — richiede ToS accettati")
def create_profile(
    payload:      CspProfileCreate,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    # Verifica ToS accettati
    org_id = uuid.UUID(current_user["org_id"])
    tos_ok = db.query(CspTosAcceptance).filter(
        CspTosAcceptance.csp_id == org_id,
        CspTosAcceptance.tos_version == TOS_VERSION_CURRENT,
    ).first()
    if not tos_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "ToS non accettati. Completare prima POST /api/v1/csp/tos/accept "
                "con tutte e cinque le clausole confermate."
            ),
        )

    existing = db.query(CspProfile).filter(
        CspProfile.organisation_id == org_id
    ).first()
    if existing:
        raise HTTPException(409, f"CSP profile già esistente: {existing.id}")

    profile = CspProfile(
        organisation_id=org_id,
        **payload.model_dump(),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    _seed_compliance_calendar(db, profile)

    from app.workers.csp_tasks import generate_csp_documents
    task = generate_csp_documents.apply_async(args=[str(profile.id)], countdown=3)

    return {
        "profile_id":  str(profile.id),
        "status":      "created",
        "doc_task_id": task.id,
        "message": (
            "Profilo CSP creato. "
            "Compliance calendar inizializzato con 15 scadenze regolamentari. "
            "Generazione 8 documenti AML/CFT via DeepSeek — pronti in ~10 minuti."
        ),
    }


@router.get("/profile", summary="Recupera profilo CSP e stato registrazione ACRA")
def get_profile(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    return _serialize_profile(_get_profile(db, current_user))


@router.patch("/profile", summary="Aggiorna profilo (ACRA registration, RQI details)")
def update_profile(
    payload:      dict,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    allowed = {
        "acra_reg_status", "acra_reg_number", "acra_reg_date", "acra_renewal_date",
        "acra_licence_type", "rqi_name", "rqi_qualification",
        "rqi_training_completed", "rqi_training_date", "rqi_acra_registration_no",
        "aml_compliance_officer", "registered_address", "business_email", "business_phone",
    }
    for k, v in payload.items():
        if k in allowed:
            setattr(profile, k, v)
    db.commit()
    return {"status": "updated", "profile_id": str(profile.id)}


# ── DASHBOARD ─────────────────────────────────────────────────────────────────
# FIX: query ottimizzate — un solo round-trip per le collection via selectinload

@router.get("/dashboard", summary="Dashboard compliance a 9 pilastri con scoring")
def get_dashboard(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    from sqlalchemy.orm import selectinload

    profile = (
        db.query(CspProfile)
        .options(
            selectinload(CspProfile.clients),
            selectinload(CspProfile.str_reports),
            selectinload(CspProfile.nominees),
            selectinload(CspProfile.nom_shareholders),
            selectinload(CspProfile.aml_programme),
            selectinload(CspProfile.training_records),
            selectinload(CspProfile.calendar),
        )
        .filter(CspProfile.organisation_id == uuid.UUID(current_user["org_id"]))
        .first()
    )
    if not profile:
        raise HTTPException(
            404, "Profilo CSP non trovato. Creare con POST /api/v1/csp/profile"
        )

    clients      = profile.clients
    str_reports  = profile.str_reports
    directors    = profile.nominees
    shareholders = profile.nom_shareholders
    training     = profile.training_records
    aml_prog     = next((p for p in profile.aml_programme if p.is_current), None)

    # CDD/EDD records — query separata (non eager-loaded sulla relazione cliente)
    from app.core.models_csp import CspCddRecord, CspEddRecord, CspBeneficialOwner
    cdd_records = db.query(CspCddRecord).filter(CspCddRecord.csp_id == profile.id).all()
    edd_records = db.query(CspEddRecord).filter(CspEddRecord.csp_id == profile.id).all()
    ubos        = db.query(CspBeneficialOwner).filter(CspBeneficialOwner.csp_id == profile.id).all()

    now = datetime.now(timezone.utc)

    score_result = compute_overall_compliance(
        profile     =_to_dict_safe(profile),
        clients     =[_to_dict_safe(c) for c in clients],
        cdd_records =[_to_dict_safe(r) for r in cdd_records],
        edd_records =[_to_dict_safe(r) for r in edd_records],
        str_reports =[_to_dict_safe(r) for r in str_reports],
        directors   =[_to_dict_safe(d) for d in directors],
        shareholders=[_to_dict_safe(s) for s in shareholders],
        ubos        =[_to_dict_safe(u) for u in ubos],
        aml_prog    =_to_dict_safe(aml_prog) if aml_prog else None,
        training    =[_to_dict_safe(t) for t in training],
    )
    profile.overall_compliance_score = score_result["overall_score"]
    profile.last_scored_at = now
    db.commit()

    calendar = profile.calendar
    upcoming = [c for c in calendar
                if c.status == "pending" and 0 <= (c.due_date - now).days <= 30]
    overdue  = [c for c in calendar
                if c.status == "pending" and c.due_date < now]

    return {
        "profile":          _serialize_profile(profile),
        "compliance_score": score_result,
        "client_stats": {
            "total":       len(clients),
            "active":      sum(1 for c in clients if c.is_active),
            "high_risk":   sum(1 for c in clients if c.risk_rating in (RiskRating.HIGH, RiskRating.VERY_HIGH)),
            "peps":        sum(1 for c in clients if c.is_pep),
            "cdd_expired": sum(1 for c in clients if c.cdd_status == CddStatus.EXPIRED),
            "cdd_pending": sum(1 for c in clients if c.cdd_status == CddStatus.NOT_STARTED),
            "sanctions_hits": sum(1 for c in clients if c.sanctions_clear is False),
        },
        "upcoming_deadlines":       [_serialize_calendar(c, now) for c in upcoming[:10]],
        "overdue_items":            [_serialize_calendar(c, now) for c in overdue[:10]],
        "open_str_decisions":       [_serialize_str(r) for r in str_reports
                                     if r.decision == StrDecision.PENDING],
        "nominees_pending_review":  [_serialize_director(d) for d in directors
                                     if d.is_active and d.next_review and d.next_review < now],
        "ubos_pending_update":      [_serialize_ubo(u) for u in ubos
                                     if u.next_review and u.next_review < now],
        "sanctions_alerts":         [_serialize_client(c) for c in clients
                                     if c.sanctions_clear is False],
    }


# ── CLIENT REGISTRY ────────────────────────────────────────────────────────────

@router.post("/clients", status_code=status.HTTP_201_CREATED,
             summary="Registra nuovo cliente — CDD obbligatoria prima di erogare servizi")
def create_client(
    payload:      CspClientCreate,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    client  = CspClient(csp_id=profile.id, **payload.model_dump())
    db.add(client)
    db.commit()
    db.refresh(client)
    return {
        "client_id":           str(client.id),
        "status":              "registered",
        "cdd_required":        True,
        "video_call_required": payload.is_remote_onboarding,
        "message": (
            "Cliente registrato. Completare CDD prima di erogare qualsiasi servizio. "
            + ("Verifica video call obbligatoria per onboarding remoto (CSP Regulations 2025 s.20)."
               if payload.is_remote_onboarding else "")
        ),
    }


@router.get("/clients", summary="Lista clienti con filtri CDD/rischio")
def list_clients(
    cdd_status:    Optional[str] = Query(None),
    risk_rating:   Optional[str] = Query(None),
    active_only:   bool          = Query(True),
    sanctions_hit: bool          = Query(False),
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    q = db.query(CspClient).filter(CspClient.csp_id == profile.id)
    if active_only:    q = q.filter(CspClient.is_active == True)
    if cdd_status:     q = q.filter(CspClient.cdd_status == cdd_status)
    if risk_rating:    q = q.filter(CspClient.risk_rating == risk_rating)
    if sanctions_hit:  q = q.filter(CspClient.sanctions_clear == False)
    clients = q.order_by(CspClient.created_at.desc()).offset(offset).limit(limit).all()
    return [_serialize_client(c) for c in clients]


@router.get("/clients/{client_id}", summary="Dettaglio cliente")
def get_client(
    client_id:    uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    return _serialize_client(_get_client(db, client_id, current_user))


# ── RISK CLASSIFICATION — INTERVENTO 2 ────────────────────────────────────────
# FIX: update_client ora obbliga motivazione + notarizza su Polygon

@router.patch(
    "/clients/{client_id}/risk",
    summary="Aggiorna risk rating cliente — notarizzato su Polygon (customer input audit)",
)
def update_client_risk(
    client_id: uuid.UUID,
    payload:   RiskClassificationCreate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    INTERVENTO 2: Ogni modifica al risk_rating viene notarizzata su Polygon.
    Il timestamp blockchain prova che la classificazione è stata confermata
    dal CSP, non generata autonomamente da Booppa.

    Questo endpoint sostituisce il generico PATCH /clients/{id} per le
    modifiche al risk_rating.
    """
    client  = _get_client(db, client_id, current_user)
    profile = _get_profile(db, current_user)
    now     = datetime.now(timezone.utc)

    previous_rating = str(client.risk_rating)

    # Aggiorna il cliente
    client.risk_rating    = payload.risk_rating
    client.risk_rationale = payload.risk_rationale

    # Snapshot dei flag al momento della classificazione
    risk_flags = {
        "is_pep":            client.is_pep,
        "high_risk_country": client.high_risk_country,
        "sanctions_clear":   client.sanctions_clear,
        "edd_required":      client.edd_required,
    }
    if payload.additional_risk_flags:
        risk_flags.update(payload.additional_risk_flags)

    # Crea audit record
    audit = CspRiskClassificationAudit(
        csp_id=profile.id,
        client_id=client.id,
        classified_by=payload.classified_by,
        classified_at=now,
        risk_rating_assigned=payload.risk_rating,
        risk_rating_previous=previous_rating,
        risk_rationale=payload.risk_rationale,
        is_pep_at_classification=client.is_pep,
        high_risk_country_at_class=client.high_risk_country,
        sanctions_clear_at_class=client.sanctions_clear,
        edd_required_at_class=client.edd_required,
        additional_risk_flags=payload.additional_risk_flags,
    )

    # Hash deterministico
    hash_content = {
        "record_type":        "risk_classification",
        "client_id":          str(client.id),
        "csp_id":             str(profile.id),
        "classified_by":      payload.classified_by,
        "classified_at":      now.isoformat(),
        "risk_rating":        payload.risk_rating,
        "risk_rating_prev":   previous_rating,
        "risk_rationale":     payload.risk_rationale,
        "risk_flags":         risk_flags,
    }
    audit.content_hash = hashlib.sha256(
        json.dumps(hash_content, sort_keys=True).encode()
    ).hexdigest()

    db.add(audit)
    db.commit()
    db.refresh(audit)

    # Notarizzazione asincrona
    from app.workers.csp_tasks import notarize_csp_record
    notarize_csp_record.apply_async(
        args=[str(audit.id), "risk_classification", str(profile.id)], countdown=3
    )

    return {
        "client_id":             str(client_id),
        "audit_id":              str(audit.id),
        "risk_rating_assigned":  payload.risk_rating,
        "risk_rating_previous":  previous_rating,
        "classified_by":         payload.classified_by,
        "classified_at":         now.isoformat(),
        "notarized":             True,
        "blockchain_tx":         None,  # disponibile dopo task async
        "polygonscan_url":       None,
        "legal_note": (
            "Classificazione di rischio notarizzata su Polygon. "
            "Il timestamp blockchain certifica che la classificazione "
            f"'{payload.risk_rating}' è stata confermata dal CSP ({payload.classified_by}) "
            f"in data {now.strftime('%Y-%m-%d %H:%M UTC')}. "
            "Qualsiasi contestazione futura sulla classificazione deve confrontarsi "
            "con questa prova on-chain."
        ),
    }


@router.patch("/clients/{client_id}", summary="Aggiorna dati cliente (non risk_rating)")
def update_client(
    client_id: uuid.UUID,
    payload:   CspClientUpdate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Aggiorna i campi non-critici del cliente.
    Per modificare risk_rating usare PATCH /clients/{id}/risk (con notarizzazione).
    """
    client = _get_client(db, client_id, current_user)
    update_data = payload.model_dump(exclude_none=True)
    # risk_rating deve passare per l'endpoint dedicato
    update_data.pop("risk_rating", None)
    for k, v in update_data.items():
        setattr(client, k, v)
    db.commit()
    return {"client_id": str(client_id), "status": "updated"}


# ── BULK IMPORT — FIX ROUTE CONFLICT ──────────────────────────────────────────
# CRITICO: questi endpoint devono stare PRIMA di /clients/{client_id}
# altrimenti FastAPI interpreta "bulk-import" come client_id → UUID parse error → 422

@router.get(
    "/bulk-import/template",  # ← prefisso /bulk-import/ — NON sotto /clients/
    summary="Download template CSV per bulk import clienti",
)
def download_bulk_import_template():
    from app.services.csp_bulk_import import generate_csv_template
    csv_bytes = generate_csv_template()
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=booppa_csp_client_import_template.csv"},
    )


@router.post(
    "/bulk-import",
    status_code=status.HTTP_201_CREATED,
    summary="Bulk import clienti da CSV o Excel (max 500 righe)",
)
async def bulk_import_clients(
    file:         UploadFile = File(...),
    auto_screen:  bool       = Form(False, description="Esegui sanctions screening durante import"),
    current_user: dict       = Depends(get_current_user),
    db                       = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    content = await file.read()

    from app.services.csp_bulk_import import parse_csv, parse_excel, execute_import

    filename = file.filename or ""
    if filename.endswith(".xlsx"):
        rows, file_errors = parse_excel(content)
    elif filename.endswith(".csv") or "text/csv" in (file.content_type or ""):
        rows, file_errors = parse_csv(content)
    else:
        rows, file_errors = parse_csv(content)

    if file_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"file_errors": file_errors},
        )

    result = await execute_import(
        rows=rows,
        csp_id=str(profile.id),
        db=db,
        auto_screen=auto_screen,
    )

    return {
        "import_summary": {
            "total_rows":      result.total_rows,
            "imported":        result.imported_count,
            "invalid_skipped": result.invalid_rows,
            "errors":          result.errors,
            "warnings":        result.warnings,
        },
        "created_client_ids": result.created_ids,
        "message": (
            f"Importati con successo {result.imported_count} di {result.total_rows} clienti. "
            + (f"{result.invalid_rows} righe saltate per errori di validazione. "
               if result.invalid_rows else "")
            + ("CDD obbligatoria per tutti i clienti prima di erogare servizi."
               if result.imported_count > 0 else "")
        ),
    }


# ── SANCTIONS SCREENING — FIX: asincrono via Celery ───────────────────────────

@router.post(
    "/clients/{client_id}/sanctions/screen",
    summary="Avvia screening sanctions — elaborazione asincrona (non blocca HTTP thread)",
)
def screen_client_sanctions(
    client_id:    uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    FIX v3: Lo screening non avviene più inline nel thread HTTP.
    Viene accodato come task Celery — risposta immediata con task_id.
    Usa GET /clients/{id}/sanctions/result per recuperare il risultato.
    """
    client  = _get_client(db, client_id, current_user)
    profile = _get_profile(db, current_user)

    from app.workers.csp_tasks import run_sanctions_screening_task
    task = run_sanctions_screening_task.apply_async(
        args=[str(client.id), str(profile.id), client.legal_name, client.client_type],
        countdown=0,
    )

    return {
        "client_id":  str(client_id),
        "task_id":    task.id,
        "status":     "queued",
        "message": (
            "Sanctions screening avviato in background. "
            f"Usare GET /api/v1/csp/clients/{client_id}/sanctions/result?task_id={task.id} "
            "per verificare il risultato (disponibile in ~5-30 secondi)."
        ),
    }


@router.get(
    "/clients/{client_id}/sanctions/result",
    summary="Recupera risultato sanctions screening",
)
def get_sanctions_result(
    client_id:    uuid.UUID,
    task_id:      str = Query(...),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """Recupera il risultato di un task sanctions screening precedentemente avviato."""
    client = _get_client(db, client_id, current_user)

    try:
        from celery.result import AsyncResult
        result = AsyncResult(task_id)
        if result.state == "PENDING":
            return {"task_id": task_id, "status": "pending", "client_id": str(client_id)}
        elif result.state == "SUCCESS":
            return {"task_id": task_id, "status": "completed", **result.result}
        elif result.state == "FAILURE":
            return {"task_id": task_id, "status": "failed",
                    "error": str(result.result)}
        else:
            return {"task_id": task_id, "status": result.state}
    except Exception as e:
        # Fallback: ritorna dati correnti dal DB
        return {
            "task_id":           task_id,
            "status":            "unknown",
            "client_id":         str(client_id),
            "sanctions_screened": client.sanctions_screened,
            "sanctions_clear":   client.sanctions_clear,
            "screened_at":       client.sanctions_screened_at.isoformat()
                                 if client.sanctions_screened_at else None,
        }


# ── CDD ────────────────────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/cdd",
             status_code=status.HTTP_201_CREATED,
             summary="Sottomette CDD — screening sanctions accodato automaticamente")
def submit_cdd(
    client_id: uuid.UUID,
    payload:   CddCreate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    client  = _get_client(db, client_id, current_user)
    now     = datetime.now(timezone.utc)

    # Enforce video call per clienti remoti
    if client.is_remote_onboarding and not payload.video_call_completed:
        if payload.id_doc_verified or payload.corp_registration_verified:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Verifica video call obbligatoria per clienti remoti "
                    "(CSP Regulations 2025 s.20). "
                    "Impostare video_call_completed=true dopo la video call live."
                ),
            )

    cdd = CspCddRecord(
        client_id=client.id,
        csp_id=profile.id,
        **{k: v for k, v in payload.model_dump().items() if hasattr(CspCddRecord, k)},
    )

    # Determina status CDD
    if payload.failure_reason:
        cdd.status        = CddStatus.FAILED
        client.cdd_status = CddStatus.FAILED
    elif payload.completed_by and (payload.id_doc_verified or payload.corp_registration_verified):
        cdd.status        = CddStatus.COMPLETED
        cdd.completed_at  = now
        client.cdd_status      = CddStatus.COMPLETED
        client.cdd_completed_at = now
        if payload.next_review_date:
            client.cdd_next_review = payload.next_review_date
        else:
            months = {"low": 12, "medium": 6, "high": 3, "very_high": 1}.get(
                str(client.risk_rating).lower().replace("riskrating.", ""), 6
            )
            client.cdd_next_review = now + timedelta(days=30 * months)
    else:
        cdd.status        = CddStatus.IN_PROGRESS
        client.cdd_status = CddStatus.IN_PROGRESS

    if payload.video_call_completed:
        client.video_call_completed = True
        client.video_call_date      = now

    db.add(cdd)
    db.commit()
    db.refresh(cdd)

    # FIX: sanctions screening asincrono (non più inline nel thread)
    screening_task_id = None
    if cdd.status == CddStatus.COMPLETED:
        name = getattr(payload, "individual_full_name", None) or client.legal_name
        from app.workers.csp_tasks import run_sanctions_screening_task, notarize_csp_record
        screening_task = run_sanctions_screening_task.apply_async(
            args=[str(client.id), str(profile.id), name, client.client_type],
            countdown=2,
        )
        screening_task_id = screening_task.id

        notarize_csp_record.apply_async(
            args=[str(cdd.id), "cdd", str(profile.id)], countdown=5
        )

    return {
        "cdd_id":                str(cdd.id),
        "status":                str(cdd.status),
        "str_assessment_required": cdd.status == CddStatus.FAILED,
        "notarization_queued":   cdd.status == CddStatus.COMPLETED,
        "sanctions_task_id":     screening_task_id,
        "message": (
            "CDD completata. Notarizzazione blockchain e sanctions screening avviati in background."
            if cdd.status == CddStatus.COMPLETED else
            "CDD fallita — valutare se presentare STR a STRO prima di procedere."
            if cdd.status == CddStatus.FAILED else
            "Record CDD salvato."
        ),
    }


@router.get("/clients/{client_id}/cdd", summary="Storico CDD per un cliente")
def get_cdd_history(
    client_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    _get_client(db, client_id, current_user)
    records = db.query(CspCddRecord).filter(
        CspCddRecord.client_id == client_id
    ).order_by(CspCddRecord.created_at.desc()).all()
    return [_serialize_cdd(r) for r in records]


# ── STR ────────────────────────────────────────────────────────────────────────

@router.post("/str", status_code=status.HTTP_201_CREATED,
             summary="Registra decisione STR — obbligatorio anche quando NON si presenta")
def log_str_decision(
    payload:      StrCreate,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    profile = _get_profile(db, current_user)

    report = CspStrReport(
        csp_id=profile.id,
        decision_date=datetime.now(timezone.utc),
        # client_notified SEMPRE False — tipping-off = reato penale CDSA s.48A
        client_notified=False,
        **{k: v for k, v in payload.model_dump().items()
           if hasattr(CspStrReport, k) and k != "client_notified"},
    )
    if payload.client_id:
        client = db.query(CspClient).filter(CspClient.id == payload.client_id).first()
        if client:
            client.str_count = (client.str_count or 0) + 1
            if payload.decision == "filed":
                client.str_filed = True

    db.add(report)
    db.commit()
    db.refresh(report)

    from app.workers.csp_tasks import notarize_csp_record
    notarize_csp_record.apply_async(
        args=[str(report.id), "str", str(profile.id)], countdown=3
    )

    return {
        "str_id":   str(report.id),
        "decision": str(payload.decision),
        "notarized": True,
        "tipping_off_reminder": (
            "AVVISO LEGALE: NON informare il cliente che è stato presentato un STR. "
            "Il tipping-off è un reato penale ai sensi del CDSA s.48A — "
            "multa fino a S$250.000 e/o reclusione fino a 3 anni."
            if payload.decision == "filed" else
            "Motivazione di non presentazione registrata e notarizzata su blockchain."
        ),
    }


@router.get("/str", summary="Lista tutte le decisioni STR")
def list_str(
    decision_filter: Optional[str] = Query(None, alias="decision"),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    q = db.query(CspStrReport).filter(CspStrReport.csp_id == profile.id)
    if decision_filter:
        q = q.filter(CspStrReport.decision == decision_filter)
    reports = q.order_by(CspStrReport.created_at.desc()).limit(100).all()
    return [_serialize_str(r) for r in reports]


# ── NOMINEES ───────────────────────────────────────────────────────────────────

@router.post("/nominees/directors", status_code=status.HTTP_201_CREATED,
             summary="Registra nominee director — fit and proper assessment richiesto")
def create_nominee_director(
    payload:      NomineeDirectorCreate,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    nominee = CspNomineeDirector(
        csp_id=profile.id,
        assessment_status=NomineeAssessment.NOT_ASSESSED,
        **payload.model_dump(),
    )
    db.add(nominee)
    db.commit()
    db.refresh(nominee)
    return {
        "nominee_id": str(nominee.id),
        "status":     "registered",
        "warning": (
            "IMPORTANTE: Il fit and proper assessment DEVE essere completato "
            "prima che questa persona possa agire come nominee director. "
            "Vedi POST /csp/nominees/directors/{id}/assess"
        ),
    }


@router.post("/nominees/directors/{nominee_id}/assess",
             summary="Registra risultato fit and proper assessment")
def assess_nominee(
    nominee_id: uuid.UUID,
    payload:    NomineeAssessmentUpdate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    nominee = db.query(CspNomineeDirector).filter(
        CspNomineeDirector.id == nominee_id,
        CspNomineeDirector.csp_id == profile.id,
    ).first()
    if not nominee:
        raise HTTPException(404, "Nominee director non trovato")

    now = datetime.now(timezone.utc)
    nominee.assessment_status      = payload.result
    nominee.assessment_date        = now
    nominee.assessed_by            = payload.assessed_by
    nominee.criminal_check_done    = payload.criminal_check_done
    nominee.bankruptcy_check_done  = payload.bankruptcy_check_done
    nominee.director_history_check = payload.director_history_check
    nominee.assessment_outcome     = payload.assessment_outcome
    nominee.assessment_notes       = payload.assessment_notes
    nominee.next_review            = _add_one_year(now)  # FIX: safe year increment
    db.commit()

    from app.workers.csp_tasks import notarize_csp_record
    notarize_csp_record.apply_async(
        args=[str(nominee_id), "nominee_assessment", str(profile.id)], countdown=3
    )

    return {
        "nominee_id": str(nominee_id),
        "result":     payload.result,
        "notarized":  True,
        "next_review": nominee.next_review.strftime("%Y-%m-%d"),
        "next_action": (
            "Assessment registrato come IDONEO. "
            "Comunicare lo status di nominee ad ACRA via BizFile (obbligatorio CLLPMA 2024)."
            if payload.result == "fit_proper" else
            "Assessment registrato come NON IDONEO. "
            "Questa persona NON può agire come nominee director (CSP Act s.15). "
            "Non procedere con l'accordo."
        ),
    }


@router.get("/nominees/directors", summary="Lista tutti i nominee directors")
def list_nominees(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    profile = _get_profile(db, current_user)
    dirs    = db.query(CspNomineeDirector).filter(
        CspNomineeDirector.csp_id == profile.id
    ).order_by(CspNomineeDirector.created_at.desc()).all()
    return [_serialize_director(d) for d in dirs]


# ── BENEFICIAL OWNERS ──────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/ubos", status_code=status.HTTP_201_CREATED,
             summary="Registra Beneficial Owner (soglia ≥25%)")
def create_ubo(
    client_id: uuid.UUID,
    payload:   UboCreate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    _get_client(db, client_id, current_user)

    from app.core.models_csp import CspBeneficialOwner
    ubo = CspBeneficialOwner(
        csp_id=profile.id,
        next_review=_add_one_year(datetime.now(timezone.utc)),  # FIX: safe year increment
        **{k: v for k, v in payload.model_dump().items() if hasattr(CspBeneficialOwner, k)},
    )
    db.add(ubo)
    db.commit()
    db.refresh(ubo)

    # Sanctions screening UBO — asincrono
    screening_task_id = None
    if payload.ubo_full_name:
        from app.workers.csp_tasks import run_sanctions_screening_task
        task = run_sanctions_screening_task.apply_async(
            args=[str(ubo.id), str(profile.id), payload.ubo_full_name, "individual"],
            kwargs={"record_type": "ubo"},
            countdown=2,
        )
        screening_task_id = task.id

    return {
        "ubo_id":             str(ubo.id),
        "status":             "registered",
        "screening_task_id":  screening_task_id,
        "next_review":        ubo.next_review.strftime("%Y-%m-%d") if ubo.next_review else None,
        "note": (
            "Sanctions screening UBO avviato in background. "
            f"Verificare risultato tramite task_id: {screening_task_id}"
            if screening_task_id else None
        ),
    }


@router.get("/clients/{client_id}/ubos", summary="Lista UBO per un cliente")
def list_ubos(client_id: uuid.UUID, current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    _get_client(db, client_id, current_user)
    from app.core.models_csp import CspBeneficialOwner
    ubos = db.query(CspBeneficialOwner).filter(CspBeneficialOwner.client_id == client_id).all()
    return [_serialize_ubo(u) for u in ubos]


# ── TRAINING ───────────────────────────────────────────────────────────────────

@router.post("/training", status_code=status.HTTP_201_CREATED,
             summary="Registra record formazione AML/CFT del personale")
def log_training(
    payload:      TrainingCreate,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    profile    = _get_profile(db, current_user)
    status_val = TrainingStatus.COMPLETED if payload.completion_date else TrainingStatus.NOT_STARTED
    record     = CspStaffTraining(
        csp_id=profile.id,
        status=status_val,
        **{k: v for k, v in payload.model_dump().items() if hasattr(CspStaffTraining, k)},
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    if payload.completion_date:
        from app.workers.csp_tasks import notarize_csp_record
        notarize_csp_record.apply_async(
            args=[str(record.id), "training", str(profile.id)], countdown=3
        )
    return {
        "training_id": str(record.id),
        "status":      str(status_val),
        "notarized":   bool(payload.completion_date),
    }


@router.get("/training", summary="Lista record formazione personale")
def list_training(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    profile = _get_profile(db, current_user)
    records = db.query(CspStaffTraining).filter(
        CspStaffTraining.csp_id == profile.id
    ).order_by(CspStaffTraining.completion_date.desc()).all()
    return [_serialize_training(r) for r in records]


# ── CALENDAR ───────────────────────────────────────────────────────────────────

@router.get("/calendar", summary="Calendario compliance regolamentare completo")
def get_calendar(
    overdue_only: bool = Query(False),
    days_ahead:   int  = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(days=days_ahead)
    q = db.query(CspComplianceCalendar).filter(
        CspComplianceCalendar.csp_id == profile.id
    )
    if overdue_only:
        q = q.filter(
            CspComplianceCalendar.due_date < now,
            CspComplianceCalendar.status == "pending",
        )
    else:
        q = q.filter(CspComplianceCalendar.due_date <= cutoff)
    items = q.order_by(CspComplianceCalendar.due_date).all()
    return [_serialize_calendar(i, now) for i in items]


@router.patch("/calendar/{item_id}/complete",
              summary="Segna un item del calendario come completato")
def complete_calendar_item(
    item_id:      uuid.UUID,
    completed_by: str,
    evidence_ref: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    item    = db.query(CspComplianceCalendar).filter(
        CspComplianceCalendar.id == item_id,
        CspComplianceCalendar.csp_id == profile.id,
    ).first()
    if not item:
        raise HTTPException(404, "Item calendario non trovato")
    item.status       = "completed"
    item.completed_at = datetime.now(timezone.utc)
    item.completed_by = completed_by
    if evidence_ref:
        item.evidence_ref = evidence_ref
    db.commit()
    return {"item_id": str(item_id), "status": "completed"}


# ── DOCUMENTS ──────────────────────────────────────────────────────────────────

@router.get("/documents", summary="Lista documenti AML/CFT generati")
def list_documents(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    profile = _get_profile(db, current_user)
    progs   = db.query(CspAmlProgramme).filter(
        CspAmlProgramme.csp_id == profile.id
    ).order_by(CspAmlProgramme.generated_at.desc()).all()
    return [{
        "id":           str(p.id),
        "version":      p.version,
        "status":       p.status,
        "is_current":   p.is_current,
        "approved_by":  p.approved_by,
        "approved_at":  p.approved_at.isoformat() if p.approved_at else None,
        "next_review":  p.next_review_date.isoformat() if p.next_review_date else None,
        "blockchain_tx": p.blockchain_tx_hash,
        "polygonscan":  p.polygonscan_url,
        "generated_at": p.generated_at.isoformat() if p.generated_at else None,
        "requires_attestation": p.status == "draft",
        "attestation_instruction": (
            "Usare POST /csp/documents/{id}/approve con attestation payload obbligatorio"
            if p.status == "draft" else None
        ),
    } for p in progs]


# ── DOCUMENTS APPROVE — INTERVENTO 1 ──────────────────────────────────────────

@router.post(
    "/documents/{programme_id}/approve",
    summary="Approva AML/CFT Programme — attestation obbligatoria + blockchain",
)
def approve_programme(
    programme_id: uuid.UUID,
    payload:      ProgrammeApprovalAttestation,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    INTERVENTO 1: L'approvazione del Programme richiede che il CSP
    confermi esplicitamente tutte e tre le dichiarazioni di responsabilità.

    Non bypassabile: Pydantic valida che tutti e tre i boolean siano True.
    L'attestazione viene notarizzata su Polygon separatamente dal documento.
    Questo crea due prove blockchain distinte:
      1. Il documento approvato (come in v2)
      2. L'attestazione del CSP (nuova in v3) — prova che il CSP ha confermato
         di essere l'unico responsabile della conformità

    Il claim "i documenti erano inadeguati e Booppa ne è responsabile" deve
    confrontarsi con questa prova on-chain dell'approvazione consapevole del CSP.
    """
    profile = _get_profile(db, current_user)
    prog    = db.query(CspAmlProgramme).filter(
        CspAmlProgramme.id == programme_id,
        CspAmlProgramme.csp_id == profile.id,
    ).first()
    if not prog:
        raise HTTPException(404, "AML/CFT Programme non trovato")

    now = datetime.now(timezone.utc)

    # Crea attestazione
    attestation = CspProgrammeAttestation(
        programme_id=programme_id,
        csp_id=profile.id,
        approved_by=payload.approved_by,
        approved_at=now,
        declaration_content_accurate=payload.declaration_content_accurate,
        declaration_legal_advice_considered=payload.declaration_legal_advice_considered,
        declaration_sole_responsible=payload.declaration_sole_responsible,
        declaration_text_shown=ATTESTATION_TEXT,
    )

    # Hash deterministico dell'attestazione
    hash_content = {
        "record_type":    "programme_attestation",
        "programme_id":   str(programme_id),
        "csp_id":         str(profile.id),
        "approved_by":    payload.approved_by,
        "approved_at":    now.isoformat(),
        "declarations":   {
            "content_accurate":        payload.declaration_content_accurate,
            "legal_advice_considered": payload.declaration_legal_advice_considered,
            "sole_responsible":        payload.declaration_sole_responsible,
        },
    }
    attestation.content_hash = hashlib.sha256(
        json.dumps(hash_content, sort_keys=True).encode()
    ).hexdigest()

    db.add(attestation)

    # Aggiorna il Programme
    prog.status          = "approved"
    prog.approved_by     = payload.approved_by
    prog.approved_at     = now
    prog.next_review_date = _add_one_year(now)  # FIX: safe year increment

    profile.aml_programme_exists   = True
    profile.aml_programme_version  = str(prog.version)
    profile.aml_programme_reviewed = now
    db.commit()
    db.refresh(attestation)

    # Due notarizzazioni distinte: documento + attestazione
    from app.workers.csp_tasks import notarize_csp_record
    notarize_csp_record.apply_async(
        args=[str(programme_id), "aml_programme_approved", str(profile.id)], countdown=3
    )
    notarize_csp_record.apply_async(
        args=[str(attestation.id), "programme_attestation", str(profile.id)], countdown=6
    )

    return {
        "programme_id":   str(programme_id),
        "status":         "approved",
        "approved_by":    payload.approved_by,
        "approved_at":    now.isoformat(),
        "attestation_id": str(attestation.id),
        "next_review":    prog.next_review_date.strftime("%Y-%m-%d"),
        "notarized":      True,
        "blockchain_tx":  None,  # disponibile dopo task async
        "polygonscan_url": None,
        "legal_message": (
            "DOCUMENTO APPROVATO. Due prove blockchain notarizzate su Polygon: "
            "(1) Il documento AML/CFT Programme approvato. "
            f"(2) L'attestazione di {payload.approved_by} che conferma di essere "
            "l'unico responsabile della conformità normativa del CSP. "
            "Qualsiasi contestazione futura deve confrontarsi con entrambe queste prove on-chain."
        ),
    }


# ── EVIDENCE LEDGER ────────────────────────────────────────────────────────────

@router.get("/evidence", summary="Ledger blockchain completo — tutti i record notarizzati")
def get_evidence(
    record_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    profile = _get_profile(db, current_user)
    q = db.query(CspBlockchainEvidence).filter(
        CspBlockchainEvidence.csp_id == profile.id
    )
    if record_type:
        q = q.filter(CspBlockchainEvidence.record_type == record_type)
    records = q.order_by(CspBlockchainEvidence.created_at.desc()).limit(limit).all()
    return [{
        "id":             str(r.id),
        "record_type":    r.record_type,
        "record_title":   r.record_title,
        "related_client": r.related_client,
        "document_hash":  r.document_hash,
        "tx_hash":        r.tx_hash,
        "block_number":   r.block_number,
        "network":        r.network,
        "timestamp":      r.blockchain_timestamp.isoformat() if r.blockchain_timestamp else None,
        "polygonscan":    r.polygonscan_url,
        "created_at":     r.created_at.isoformat(),
    } for r in records]


# ── PRIVATE HELPERS ────────────────────────────────────────────────────────────

def _get_profile(db, current_user) -> CspProfile:
    p = db.query(CspProfile).filter(
        CspProfile.organisation_id == uuid.UUID(current_user["org_id"])
    ).first()
    if not p:
        raise HTTPException(
            404,
            "Profilo CSP non trovato. Creare con POST /api/v1/csp/profile"
        )
    return p


def _get_profile_optional(db, current_user):
    """Come _get_profile ma non lancia eccezione se non trovato."""
    try:
        return _get_profile(db, current_user)
    except HTTPException:
        return None


def _get_client(db, client_id: uuid.UUID, current_user) -> CspClient:
    profile = _get_profile(db, current_user)
    c = db.query(CspClient).filter(
        CspClient.id == client_id,
        CspClient.csp_id == profile.id,
    ).first()
    if not c:
        raise HTTPException(404, "Cliente non trovato")
    return c


def _to_dict_safe(obj) -> dict:
    """
    FIX v3: Serializza un ORM object escludendo i campi cifrati (EncryptedString/Text).
    Evita che valori 'ENC:...' vengano passati al compliance scorer causando
    confronti silenziosamente errati.

    I campi cifrati non sono usati dalla logica di scoring — solo dall'UI/output.
    """
    if obj is None:
        return {}

    # Campi cifrati da escludere dal dict usato nel scorer
    ENCRYPTED_FIELDS = {
        "individual_nric_or_passport",
        "individual_address",
        "nominee_nric_or_passport",
        "nominee_address",
        "nominator_id",
        "ubo_nric_or_passport",
        "ubo_address",
    }

    result = {}
    for c in obj.__table__.columns:
        if c.key in ENCRYPTED_FIELDS:
            continue  # skip campi cifrati
        result[c.key] = getattr(obj, c.key)
    return result


def _serialize_profile(p: CspProfile) -> dict:
    services = [
        k.replace("offers_", "").replace("_", " ").title()
        for k in ["offers_company_formation", "offers_nominee_director",
                  "offers_nominee_shareholder", "offers_registered_address",
                  "offers_corp_secretarial", "offers_shelf_company"]
        if getattr(p, k, False)
    ]
    return {
        "id": str(p.id), "legal_name": p.legal_name, "uen": p.uen,
        "acra_reg_status":     str(p.acra_reg_status),
        "acra_reg_number":     p.acra_reg_number,
        "acra_renewal_date":   p.acra_renewal_date.isoformat() if p.acra_renewal_date else None,
        "rqi_name":            p.rqi_name,
        "rqi_qualification":   p.rqi_qualification,
        "rqi_training_completed": p.rqi_training_completed,
        "aml_compliance_officer": p.aml_compliance_officer,
        "aml_programme_exists":   p.aml_programme_exists,
        "overall_compliance_score": p.overall_compliance_score,
        "last_scored_at": p.last_scored_at.isoformat() if p.last_scored_at else None,
        "csp_pack_tier":  p.csp_pack_tier,
        "services":       services,
        "created_at":     p.created_at.isoformat() if p.created_at else None,
    }


def _serialize_client(c: CspClient) -> dict:
    return {
        "id": str(c.id), "client_type": c.client_type,
        "legal_name": c.legal_name, "uen_or_reg_no": c.uen_or_reg_no,
        "country_of_inc": c.country_of_inc, "contact_name": c.contact_name,
        "contact_email": c.contact_email,
        "risk_rating": str(c.risk_rating), "cdd_status": str(c.cdd_status),
        "cdd_completed_at": c.cdd_completed_at.isoformat() if c.cdd_completed_at else None,
        "cdd_next_review":  c.cdd_next_review.isoformat() if c.cdd_next_review else None,
        "edd_required": c.edd_required, "is_pep": c.is_pep,
        "high_risk_country": c.high_risk_country,
        "is_remote_onboarding": c.is_remote_onboarding,
        "video_call_completed": c.video_call_completed,
        "has_nominee_director":    c.has_nominee_director,
        "has_nominee_shareholder": c.has_nominee_shareholder,
        "str_filed": c.str_filed, "str_count": c.str_count or 0,
        "sanctions_screened": c.sanctions_screened,
        "sanctions_clear":    c.sanctions_clear,
        "sanctions_screened_at": c.sanctions_screened_at.isoformat()
                                  if c.sanctions_screened_at else None,
        "is_active": c.is_active,
        "onboarded_at": c.onboarded_at.isoformat() if c.onboarded_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _serialize_cdd(r: CspCddRecord) -> dict:
    return {
        "id": str(r.id), "client_id": str(r.client_id),
        "review_type": r.review_type, "status": str(r.status),
        "completed_by": r.completed_by,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "next_review_date": r.next_review_date.isoformat() if r.next_review_date else None,
        "failure_reason": r.failure_reason,
        "sanctions_screened": r.sanctions_screened,
        "sanctions_clear": r.sanctions_clear,
        "pep_screening_done": r.pep_screening_done, "pep_result": r.pep_result,
        "non_face_to_face": r.non_face_to_face,
        "video_call_completed": r.video_call_completed,
        "blockchain_tx_hash": r.blockchain_tx_hash,
        "polygonscan_url": r.polygonscan_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize_str(r: CspStrReport) -> dict:
    return {
        "id": str(r.id), "client_id": str(r.client_id) if r.client_id else None,
        "trigger_type": r.trigger_type, "trigger_detail": r.trigger_detail,
        "decision": str(r.decision), "decision_by": r.decision_by,
        "decision_date": r.decision_date.isoformat() if r.decision_date else None,
        "decision_rationale": r.decision_rationale,
        "stro_reference": r.stro_reference,
        "stro_filed_date": r.stro_filed_date.isoformat() if r.stro_filed_date else None,
        "service_declined": r.service_declined,
        "client_notified": r.client_notified,   # always False
        "blockchain_tx_hash": r.blockchain_tx_hash,
        "polygonscan_url": r.polygonscan_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize_director(d: CspNomineeDirector) -> dict:
    return {
        "id": str(d.id), "client_id": str(d.client_id),
        "nominee_full_name": d.nominee_full_name,
        "nominator_name": d.nominator_name,
        "company_name": d.company_name, "company_uen": d.company_uen,
        "assessment_status": str(d.assessment_status),
        "assessment_date": d.assessment_date.isoformat() if d.assessment_date else None,
        "acra_disclosed": d.acra_disclosed,
        "acra_filing_date": d.acra_filing_date.isoformat() if d.acra_filing_date else None,
        "is_active": d.is_active,
        "next_review": d.next_review.isoformat() if d.next_review else None,
        "blockchain_tx_hash": d.blockchain_tx_hash,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _serialize_ubo(u) -> dict:
    return {
        "id": str(u.id), "client_id": str(u.client_id),
        "ubo_full_name": u.ubo_full_name, "ubo_nationality": u.ubo_nationality,
        "ownership_percentage": u.ownership_percentage,
        "control_mechanism": u.control_mechanism,
        "is_pep": u.is_pep, "is_sanctioned": u.is_sanctioned,
        "identity_verified": u.identity_verified,
        "verification_date": u.verification_date.isoformat() if u.verification_date else None,
        "next_review": u.next_review.isoformat() if u.next_review else None,
        "blockchain_tx_hash": u.blockchain_tx_hash,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _serialize_training(t: CspStaffTraining) -> dict:
    return {
        "id": str(t.id), "staff_name": t.staff_name, "staff_role": t.staff_role,
        "is_rqi": t.is_rqi, "training_type": t.training_type,
        "training_title": t.training_title, "provider": t.provider,
        "completion_date": t.completion_date.isoformat() if t.completion_date else None,
        "expiry_date": t.expiry_date.isoformat() if t.expiry_date else None,
        "status": str(t.status), "score": t.score,
        "blockchain_tx_hash": t.blockchain_tx_hash,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _serialize_calendar(c: CspComplianceCalendar, now: datetime) -> dict:
    days = (c.due_date - now).days if c.due_date else None
    return {
        "id": str(c.id), "pillar": str(c.pillar), "title": c.title,
        "description": c.description,
        "due_date": c.due_date.isoformat() if c.due_date else None,
        "frequency": c.frequency, "legal_basis": c.legal_basis,
        "penalty_if_missed": c.penalty_if_missed,
        "status": c.status,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "days_remaining": days,
        "is_overdue": days is not None and days < 0,
    }


def _seed_compliance_calendar(db, profile: CspProfile):
    """Inizializza tutte le scadenze regolamentari obbligatorie per un nuovo profilo CSP."""
    now = datetime.now(timezone.utc)
    items = [
        ("acra_registration", "Rinnovo Annuale Licenza ACRA CSP",
         "CSP Act s.7 — rinnovo annuale obbligatorio",
         "Multa S$50.000 o 2 anni di reclusione",
         _add_one_year(now), "annual"),
        ("acra_registration", "Notifica ACRA di Modifiche Materiali",
         "Entro 14 giorni da qualsiasi modifica materiale a soci, sede o servizi",
         "Multa fino a S$25.000", now + timedelta(days=14), "triggered"),
        ("aml_cft_programme", "Revisione Annuale Programma AML/CFT/PF",
         "CSP Act — programma rivisto annualmente o al cambio normativo",
         "S$100.000 per breach", _add_one_year(now), "annual"),
        ("aml_cft_programme", "Approvazione Senior Management Programma AML/CFT",
         "Approvazione iniziale del senior management obbligatoria",
         "N/A — prerequisito per registrazione ACRA", now + timedelta(days=30), "once"),
        ("cdd", "Revisione Trimestrale CDD — Clienti ALTO Rischio",
         "Approccio basato sul rischio — clienti ALTO richiedono revisione trimestrale",
         "S$100.000 per breach", now + timedelta(days=90), "quarterly"),
        ("cdd", "Revisione Semestrale CDD — Clienti MEDIO Rischio",
         "Approccio basato sul rischio — clienti MEDIO rivisti ogni 6 mesi",
         "S$100.000 per breach", now + timedelta(days=180), "semi-annual"),
        ("cdd", "Revisione Annuale CDD — Clienti BASSO Rischio",
         "Approccio basato sul rischio — clienti BASSO rivisti annualmente",
         "S$100.000 per breach", _add_one_year(now), "annual"),
        ("nominee_management", "Revisione Annuale Fit & Proper Nominee Director",
         "CSP Act s.15 — revisione annuale di tutti i nominee director attivi",
         "S$100.000 per breach", _add_one_year(now), "annual"),
        ("nominee_management", "Filing Disclosure Nominee ad ACRA",
         "CLLPMA 2024 — comunicare status nominee director/shareholder ad ACRA",
         "Multa fino a S$25.000", now + timedelta(days=30), "triggered"),
        ("beneficial_ownership", "Aggiornamento Annuale Registro UBO",
         "Obblighi CDD CSP Act — verificare informazioni UBO annualmente",
         "S$25.000 per breach", _add_one_year(now), "annual"),
        ("staff_training", "Formazione Annuale AML/CFT RQI",
         "CSP Act s.9 — RQI deve completare formazione AML/CFT/PF annuale",
         "Licenza CSP non valida", _add_one_year(now), "annual"),
        ("staff_training", "Formazione Annuale AML/CFT Tutto il Personale",
         "CSP Act — tutto il personale che gestisce servizi regolamentati richiede formazione annuale",
         "S$100.000 per breach", _add_one_year(now), "annual"),
        ("pdpa_nric", "Scadenza Divieto Autenticazione NRIC",
         "PDPA s.13 + Advisory PDPC Set 2024 — rimuovere tutti gli usi di autenticazione NRIC",
         "S$1.000.000 o 10% del fatturato annuo",
         datetime(2026, 12, 31, tzinfo=timezone.utc), "once"),
        ("pdpa_nric", "Revisione Annuale DPMP",
         "PDPA s.11 — Data Protection Management Programme rivisto annualmente",
         "S$1.000.000 o 10% del fatturato annuo",
         _add_one_year(now), "annual"),
        ("record_keeping", "Audit Retention 5 Anni",
         "CSP Act s.27 — verificare records prossimi al limite di retention obbligatoria",
         "Responsabilità penale per distruzione prematura",
         _add_one_year(now), "annual"),
    ]
    for pillar, title, desc, penalty, due, freq in items:
        db.add(CspComplianceCalendar(
            csp_id=profile.id, pillar=pillar, title=title,
            description=desc, penalty_if_missed=penalty,
            due_date=due, frequency=freq, status="pending",
        ))
    db.commit()
