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

from app.core.db import get_db, get_current_user
from app.core.config import settings
from app.core.models import User
from app.core.models_enterprise import (
    Organisation, OrganisationMember, Subsidiary,
    WebhookEndpoint, WebhookDelivery,
    TrmControl, TrmEvidence,
    RetentionPolicy, SsoConfig, WhiteLabelConfig, SlaLog,
    OrganisationInvite, VendorWatchlistItem, VendorWatchlistComment,
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


class InviteCreate(BaseModel):
    email: str
    role: str = "member"  # admin | member


class WatchlistCreate(BaseModel):
    vendor_ref: str
    vendor_name: Optional[str] = None
    notes: Optional[str] = None


class WatchlistUpdate(BaseModel):
    vendor_name: Optional[str] = None
    notes: Optional[str] = None


class WatchlistCommentCreate(BaseModel):
    body: str


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


# ── 9. Members ────────────────────────────────────────────────────────────────

@router.get("/organisations/{org_id}/members")
def list_members(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    rows = (
        db.query(OrganisationMember, User)
        .join(User, User.id == OrganisationMember.user_id)
        .filter(OrganisationMember.organisation_id == org_id)
        .all()
    )
    return [
        {"user_id": str(u.id), "email": u.email, "role": m.role, "joined_at": m.created_at.isoformat() if m.created_at else None}
        for m, u in rows
    ]


@router.delete("/organisations/{org_id}/members/{user_id}", status_code=204)
def remove_member(org_id: str, user_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    org = _get_org(org_id, current_user, db)
    if str(org.owner_user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Only the org owner can remove members")
    if str(org.owner_user_id) == str(user_id):
        raise HTTPException(status_code=400, detail="Cannot remove the org owner")
    member = db.query(OrganisationMember).filter(
        OrganisationMember.organisation_id == org_id,
        OrganisationMember.user_id == user_id,
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(member)
    db.commit()


# ── 10. Invites ───────────────────────────────────────────────────────────────

INVITE_TTL_DAYS = 7


@router.post("/organisations/{org_id}/invites", status_code=201)
def create_invite(org_id: str, body: InviteCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from datetime import datetime, timedelta
    org = _get_org(org_id, current_user, db)

    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    if body.role not in {"admin", "member"}:
        raise HTTPException(status_code=400, detail="role must be admin or member")

    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        already_member = db.query(OrganisationMember).filter(
            OrganisationMember.organisation_id == org_id,
            OrganisationMember.user_id == existing_user.id,
        ).first()
        if already_member:
            raise HTTPException(status_code=409, detail="User is already a member")

    existing_invite = db.query(OrganisationInvite).filter(
        OrganisationInvite.organisation_id == org_id,
        OrganisationInvite.email == email,
        OrganisationInvite.status == "pending",
    ).first()
    if existing_invite:
        raise HTTPException(status_code=409, detail="A pending invite already exists for this email")

    token = secrets.token_urlsafe(32)
    invite = OrganisationInvite(
        id=uuid.uuid4(),
        organisation_id=org_id,
        email=email,
        role=body.role,
        token=token,
        invited_by_user_id=current_user.id,
        expires_at=datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)

    # Best-effort email notification
    try:
        import asyncio as _asyncio
        from app.services.email_service import EmailService
        accept_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/orgs/invites/{token}"
        body_html = f"""<html><body style="font-family:Arial;max-width:600px;margin:0 auto;color:#0f172a;">
        <div style="padding:32px;border:1px solid #e2e8f0;border-radius:12px;">
          <h1 style="font-size:20px;margin:0 0 16px;">You've been invited to {org.name} on BOOPPA</h1>
          <p>{current_user.email} invited you to join <strong>{org.name}</strong> as a {body.role}.</p>
          <a href="{accept_url}"
             style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                    border-radius:8px;font-weight:bold;display:inline-block;margin-top:12px;">
            Accept invite
          </a>
          <p style="margin-top:24px;font-size:12px;color:#94a3b8;">This invite expires in {INVITE_TTL_DAYS} days.</p>
        </div></body></html>"""
        _asyncio.run(EmailService().send_html_email(
            to_email=email,
            subject=f"Invitation to join {org.name} on BOOPPA",
            body_html=body_html,
        ))
    except Exception as exc:
        logger.warning(f"[OrgInvite] Email failed for {email}: {exc}")

    return {
        "id": str(invite.id),
        "email": invite.email,
        "role": invite.role,
        "expires_at": invite.expires_at.isoformat(),
        "token": token,  # surfaced once for testing/manual sharing
    }


@router.get("/organisations/{org_id}/invites")
def list_invites(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    invites = db.query(OrganisationInvite).filter(
        OrganisationInvite.organisation_id == org_id,
        OrganisationInvite.status == "pending",
    ).all()
    return [
        {"id": str(i.id), "email": i.email, "role": i.role, "expires_at": i.expires_at.isoformat()}
        for i in invites
    ]


@router.delete("/organisations/{org_id}/invites/{invite_id}", status_code=204)
def revoke_invite(org_id: str, invite_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    invite = db.query(OrganisationInvite).filter(
        OrganisationInvite.id == invite_id,
        OrganisationInvite.organisation_id == org_id,
    ).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    invite.status = "revoked"
    db.commit()


@router.post("/invites/{token}/accept", status_code=201)
def accept_invite(token: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from datetime import datetime
    invite = db.query(OrganisationInvite).filter(OrganisationInvite.token == token).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.status != "pending":
        raise HTTPException(status_code=410, detail=f"Invite is {invite.status}")
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        invite.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="Invite expired")
    if (current_user.email or "").strip().lower() != invite.email:
        raise HTTPException(status_code=403, detail="This invite was issued to a different email")

    already = db.query(OrganisationMember).filter(
        OrganisationMember.organisation_id == invite.organisation_id,
        OrganisationMember.user_id == current_user.id,
    ).first()
    if not already:
        db.add(OrganisationMember(
            id=uuid.uuid4(),
            organisation_id=invite.organisation_id,
            user_id=current_user.id,
            role=invite.role,
        ))

    invite.status = "accepted"
    invite.accepted_at = datetime.utcnow()
    invite.accepted_user_id = current_user.id
    db.commit()
    return {"organisation_id": str(invite.organisation_id), "role": invite.role}


# ── 11. Shared vendor watchlist ───────────────────────────────────────────────

@router.get("/organisations/{org_id}/watchlist")
def list_watchlist(org_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    items = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.organisation_id == org_id
    ).order_by(VendorWatchlistItem.created_at.desc()).all()
    return [
        {
            "id": str(i.id),
            "vendor_ref": i.vendor_ref,
            "vendor_name": i.vendor_name,
            "notes": i.notes,
            "added_by_user_id": str(i.added_by_user_id),
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in items
    ]


@router.post("/organisations/{org_id}/watchlist", status_code=201)
def add_watchlist_item(org_id: str, body: WatchlistCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    vendor_ref = body.vendor_ref.strip()
    if not vendor_ref:
        raise HTTPException(status_code=400, detail="vendor_ref required")

    duplicate = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.organisation_id == org_id,
        VendorWatchlistItem.vendor_ref == vendor_ref,
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Vendor already on watchlist")

    item = VendorWatchlistItem(
        id=uuid.uuid4(),
        organisation_id=org_id,
        vendor_ref=vendor_ref,
        vendor_name=body.vendor_name,
        notes=body.notes,
        added_by_user_id=current_user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": str(item.id), "vendor_ref": item.vendor_ref}


@router.patch("/organisations/{org_id}/watchlist/{item_id}")
def update_watchlist_item(org_id: str, item_id: str, body: WatchlistUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    item = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.id == item_id,
        VendorWatchlistItem.organisation_id == org_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    if body.vendor_name is not None:
        item.vendor_name = body.vendor_name
    if body.notes is not None:
        item.notes = body.notes
    db.commit()
    return {"id": str(item.id), "vendor_name": item.vendor_name, "notes": item.notes}


@router.delete("/organisations/{org_id}/watchlist/{item_id}", status_code=204)
def remove_watchlist_item(org_id: str, item_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    item = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.id == item_id,
        VendorWatchlistItem.organisation_id == org_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    db.delete(item)
    db.commit()


@router.get("/organisations/{org_id}/watchlist/{item_id}/comments")
def list_watchlist_comments(org_id: str, item_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    item = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.id == item_id,
        VendorWatchlistItem.organisation_id == org_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    comments = db.query(VendorWatchlistComment).filter(
        VendorWatchlistComment.watchlist_item_id == item_id
    ).order_by(VendorWatchlistComment.created_at.asc()).all()
    return [
        {
            "id": str(c.id),
            "author_user_id": str(c.author_user_id),
            "body": c.body,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]


@router.post("/organisations/{org_id}/watchlist/{item_id}/comments", status_code=201)
def add_watchlist_comment(org_id: str, item_id: str, body: WatchlistCommentCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_org(org_id, current_user, db)
    item = db.query(VendorWatchlistItem).filter(
        VendorWatchlistItem.id == item_id,
        VendorWatchlistItem.organisation_id == org_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="body required")
    comment = VendorWatchlistComment(
        id=uuid.uuid4(),
        watchlist_item_id=item_id,
        author_user_id=current_user.id,
        body=text,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return {"id": str(comment.id), "body": comment.body}


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
