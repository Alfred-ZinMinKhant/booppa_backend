"""
Enterprise API — V12
17 endpoints under /api/v1/enterprise/
"""
import secrets
import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.config import settings
from app.api.auth import get_current_user
from app.core.models import User
from app.core.models_enterprise import (
    Organisation, OrganisationMember, Subsidiary,
    WebhookEndpoint, WebhookDelivery,
    TrmControl, TrmEvidence,
    RetentionPolicy, SsoConfig, WhiteLabelConfig, SlaLog,
    MAS_TRM_DOMAINS,
)

router = APIRouter()
sso_router = APIRouter()
logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class OrgCreate(BaseModel):
    name: str
    slug: str
    tier: str = "standard"

class OrgUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None

class SubsidiaryCreate(BaseModel):
    name: str
    uen: Optional[str] = None
    country: str = "Singapore"

class WebhookCreate(BaseModel):
    url: str
    events: List[str] = []

class TrmControlUpdate(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None

class TrmGapRequest(BaseModel):
    control_id: str
    context: str

class RetentionPolicyCreate(BaseModel):
    data_category: str
    retention_days: int
    auto_purge: bool = False

class SsoConfigCreate(BaseModel):
    protocol: str                      # saml | oidc
    idp_metadata_url: Optional[str] = None
    idp_entity_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    discovery_url: Optional[str] = None

class WhiteLabelUpdate(BaseModel):
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    footer_text: Optional[str] = None
    report_header_text: Optional[str] = None
    custom_domain: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_org(org_id: str, user: User, db: Session) -> Organisation:
    org = db.query(Organisation).filter(Organisation.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    member = db.query(OrganisationMember).filter(
        OrganisationMember.organisation_id == org_id,
        OrganisationMember.user_id == user.id,
    ).first()
    if not member and str(org.owner_user_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    return org


# ── 1. Org CRUD ───────────────────────────────────────────────────────────────

@router.post("/activate", status_code=201)
def activate_organisation(body: OrgCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Create org + initialise 13 MAS TRM controls."""
    if db.query(Organisation).filter(Organisation.slug == body.slug).first():
        raise HTTPException(status_code=409, detail="Slug already taken")
    org = Organisation(
        id=uuid.uuid4(),
        name=body.name,
        slug=body.slug,
        tier=body.tier,
        owner_user_id=current_user.id,
    )
    db.add(org)
    db.flush()

    # Auto-add owner as member
    db.add(OrganisationMember(id=uuid.uuid4(), organisation_id=org.id, user_id=current_user.id, role="owner"))

    # Initialise TRM controls
    from app.trm_workflow_service import initialise_trm_controls
    initialise_trm_controls(str(org.id), db)

    db.commit()
    db.refresh(org)
    return {"id": str(org.id), "slug": org.slug, "tier": org.tier, "trm_controls_created": len(MAS_TRM_DOMAINS)}


@router.get("/organisations")
def list_organisations(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """List all orgs the current user belongs to."""
    memberships = db.query(OrganisationMember).filter(OrganisationMember.user_id == current_user.id).all()
    org_ids = [m.organisation_id for m in memberships]
    orgs = db.query(Organisation).filter(Organisation.id.in_(org_ids)).all()
    return [{"id": str(o.id), "name": o.name, "slug": o.slug, "tier": o.tier} for o in orgs]


@router.get("/organisations/{org_id}")
def get_organisation(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    org = _get_org(org_id, current_user, db)
    return {"id": str(org.id), "name": org.name, "slug": org.slug, "tier": org.tier, "is_active": org.is_active}


@router.patch("/organisations/{org_id}")
def update_organisation(org_id: str, body: OrgUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    org = _get_org(org_id, current_user, db)
    if body.name:
        org.name = body.name
    if body.tier:
        org.tier = body.tier
    db.commit()
    return {"id": str(org.id), "name": org.name, "tier": org.tier}


# ── 2. Subsidiaries ───────────────────────────────────────────────────────────

@router.post("/organisations/{org_id}/subsidiaries", status_code=201)
def add_subsidiary(org_id: str, body: SubsidiaryCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    sub = Subsidiary(id=uuid.uuid4(), organisation_id=org_id, **body.model_dump())
    db.add(sub)
    db.commit()
    return {"id": str(sub.id), "name": sub.name}


@router.get("/organisations/{org_id}/subsidiaries")
def list_subsidiaries(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    subs = db.query(Subsidiary).filter(Subsidiary.organisation_id == org_id).all()
    return [{"id": str(s.id), "name": s.name, "uen": s.uen, "country": s.country} for s in subs]


# ── 3. Webhooks ───────────────────────────────────────────────────────────────

@router.post("/organisations/{org_id}/webhooks", status_code=201)
def create_webhook(org_id: str, body: WebhookCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    ep = WebhookEndpoint(
        id=uuid.uuid4(),
        organisation_id=org_id,
        url=body.url,
        secret=secrets.token_hex(32),
        events=body.events,
    )
    db.add(ep)
    db.commit()
    return {"id": str(ep.id), "url": ep.url, "secret": ep.secret, "events": ep.events}


@router.get("/organisations/{org_id}/webhooks")
def list_webhooks(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    eps = db.query(WebhookEndpoint).filter(WebhookEndpoint.organisation_id == org_id).all()
    return [{"id": str(e.id), "url": e.url, "events": e.events, "is_active": e.is_active} for e in eps]


@router.delete("/organisations/{org_id}/webhooks/{webhook_id}", status_code=204)
def delete_webhook(org_id: str, webhook_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    ep = db.query(WebhookEndpoint).filter(WebhookEndpoint.id == webhook_id, WebhookEndpoint.organisation_id == org_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Webhook not found")
    db.delete(ep)
    db.commit()


# ── 4. MAS TRM Controls ───────────────────────────────────────────────────────

@router.get("/organisations/{org_id}/trm")
def list_trm_controls(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    controls = db.query(TrmControl).filter(TrmControl.organisation_id == org_id).all()
    return [{"id": str(c.id), "domain": c.domain, "control_ref": c.control_ref, "status": c.status, "risk_rating": c.risk_rating, "gap_analysis": c.gap_analysis} for c in controls]


@router.patch("/organisations/{org_id}/trm/{control_id}")
def update_trm_control(org_id: str, control_id: str, body: TrmControlUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    ctrl = db.query(TrmControl).filter(TrmControl.id == control_id, TrmControl.organisation_id == org_id).first()
    if not ctrl:
        raise HTTPException(status_code=404, detail="Control not found")
    if body.status:
        ctrl.status = body.status
    if body.description:
        ctrl.description = body.description
    db.commit()
    return {"id": str(ctrl.id), "status": ctrl.status}


@router.post("/organisations/{org_id}/trm/gap-analysis")
async def run_gap_analysis(org_id: str, body: TrmGapRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    ctrl = db.query(TrmControl).filter(TrmControl.id == body.control_id, TrmControl.organisation_id == org_id).first()
    if not ctrl:
        raise HTTPException(status_code=404, detail="Control not found")
    from app.trm_workflow_service import run_gap_analysis as _run
    ctrl = await _run(ctrl, body.context, db)
    return {"id": str(ctrl.id), "domain": ctrl.domain, "gap_analysis": ctrl.gap_analysis, "risk_rating": ctrl.risk_rating, "status": ctrl.status}


# ── 5. Retention Policies ─────────────────────────────────────────────────────

@router.post("/organisations/{org_id}/retention", status_code=201)
def create_retention_policy(org_id: str, body: RetentionPolicyCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    policy = RetentionPolicy(id=uuid.uuid4(), organisation_id=org_id, **body.model_dump())
    db.add(policy)
    db.commit()
    return {"id": str(policy.id), "data_category": policy.data_category, "retention_days": policy.retention_days}


@router.get("/organisations/{org_id}/retention")
def list_retention_policies(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    policies = db.query(RetentionPolicy).filter(RetentionPolicy.organisation_id == org_id).all()
    return [{"id": str(p.id), "data_category": p.data_category, "retention_days": p.retention_days, "auto_purge": p.auto_purge} for p in policies]


# ── 6. White-label ────────────────────────────────────────────────────────────

@router.put("/organisations/{org_id}/white-label")
def upsert_white_label(org_id: str, body: WhiteLabelUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    cfg = db.query(WhiteLabelConfig).filter(WhiteLabelConfig.organisation_id == org_id).first()
    if not cfg:
        cfg = WhiteLabelConfig(id=uuid.uuid4(), organisation_id=org_id)
        db.add(cfg)
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, val)
    db.commit()
    return {"organisation_id": org_id, "primary_color": cfg.primary_color, "custom_domain": cfg.custom_domain}


# ── 7. SSO Config ─────────────────────────────────────────────────────────────

@router.put("/organisations/{org_id}/sso")
def upsert_sso_config(org_id: str, body: SsoConfigCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    cfg = db.query(SsoConfig).filter(SsoConfig.organisation_id == org_id).first()
    if not cfg:
        cfg = SsoConfig(id=uuid.uuid4(), organisation_id=org_id)
        db.add(cfg)
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, val)
    db.commit()
    from app.white_label_and_sso import get_saml_acs_url
    from app.core.models_enterprise import Organisation as _Org
    org = db.query(_Org).filter(_Org.id == org_id).first()
    return {"organisation_id": org_id, "protocol": cfg.protocol, "is_active": cfg.is_active, "acs_url": get_saml_acs_url(org.slug) if cfg.protocol == "saml" else None}


# ── 8. SLA Logs ───────────────────────────────────────────────────────────────

@router.get("/organisations/{org_id}/sla")
def list_sla_logs(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    logs = db.query(SlaLog).filter(SlaLog.organisation_id == org_id).order_by(SlaLog.recorded_at.desc()).limit(100).all()
    return [{"id": str(l.id), "event_type": l.event_type, "target_minutes": l.target_minutes, "actual_minutes": l.actual_minutes, "met": l.met} for l in logs]


# ── SSO router — OIDC callback ────────────────────────────────────────────────

@sso_router.get("/sso/oidc/callback")
async def oidc_callback(code: str, state: str, db: Session = Depends(get_db)):
    """OIDC authorisation code callback — exchanges code for tokens."""
    org_id = state  # state carries org_id; in production use a signed state token
    cfg = db.query(SsoConfig).filter(SsoConfig.organisation_id == org_id, SsoConfig.is_active == True).first()
    if not cfg:
        raise HTTPException(status_code=400, detail="SSO not configured for this organisation")
    from app.white_label_and_sso import exchange_oidc_code, get_saml_acs_url
    from app.core.config import settings as _s
    redirect_uri = f"{_s.VERIFY_BASE_URL.rstrip('/')}/api/v1/enterprise/sso/oidc/callback"
    tokens = await exchange_oidc_code(cfg, code, redirect_uri)
    if not tokens:
        raise HTTPException(status_code=400, detail="Token exchange failed")
    return {"access_token": tokens.get("access_token"), "id_token": tokens.get("id_token")}


@sso_router.post("/sso/saml/acs/{org_slug}")
async def saml_acs(org_slug: str):
    """SAML 2.0 ACS endpoint stub — integrate a SAML library for production use."""
    return {"message": f"SAML ACS for {org_slug} — integrate python3-saml or pysaml2 for full SAML processing"}
