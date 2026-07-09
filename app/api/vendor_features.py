"""
Vendor enterprise features — API keys, webhooks, multi-subsidiary, SSO.

Webhooks/SSO/membership use the existing org-keyed enterprise models
(`models_enterprise.py`). Each subscribing user gets a lazily-created
Organisation so the API surfaces as user-centric while storage remains
properly tenant-scoped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as _secrets
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User
from app.core.models import ApiKey
from app.core.models import (
    Organisation, OrganisationMember, WebhookEndpoint, WebhookDelivery, SsoConfig,
)
from app.billing.enforcement import enforce_tier

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _features(user: User) -> dict:
    result = enforce_tier(
        assessment_data={
            "plan": (user.plan or "free").lower(),
            "subscription_status": "active" if user.stripe_subscription_id else "inactive",
            "payment_confirmed": bool(user.stripe_subscription_id),
        },
        framework=None,
    )
    return result.get("features") or {}


def _require_feature(user: User, key: str, label: str) -> None:
    if not _features(user).get(key):
        raise HTTPException(
            status_code=403,
            detail=f"{label} is not included in your current plan. Upgrade to unlock.",
        )


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "tenant"


def _get_or_create_org(db: Session, user: User) -> Organisation:
    """Find the Organisation this user owns, or create one lazily.

    Vendors subscribing to Standard/Pro Suite get an org auto-provisioned on
    first feature use so we don't need to migrate every subscriber.
    """
    org = db.query(Organisation).filter(Organisation.owner_user_id == user.id).first()
    if org:
        return org
    base_slug = _slugify(user.company or user.email.split("@")[0])
    slug = base_slug
    suffix = 1
    while db.query(Organisation).filter(Organisation.slug == slug).first():
        suffix += 1
        slug = f"{base_slug}-{suffix}"
    tier = "pro" if (user.plan or "").startswith("pro") else "standard"
    # Seed sector from the user's industry (normalised to a known TRM sector key)
    # so the TRM baseline + workspace order domains by sector criticality without
    # needing a separate intake step.
    from app.services.trm_sector_override import normalise_sector
    org = Organisation(
        name=user.company or user.full_name or user.email,
        slug=slug,
        tier=tier,
        sector=normalise_sector(getattr(user, "industry", None)),
        owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    # Owner becomes the first member
    db.add(OrganisationMember(
        organisation_id=org.id, user_id=user.id, role="owner",
    ))
    db.commit()
    return org


# ═══════════════════════════════════════════════════════════════════════════════
# API KEYS (user-scoped)
# ═══════════════════════════════════════════════════════════════════════════════

class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


@router.get("/api-keys")
def list_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "api_access", "API access")
    rows = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == current_user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return {"items": [
        {
            "id": str(r.id), "name": r.name, "prefix": r.prefix,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
        }
        for r in rows
    ]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: ApiKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "api_access", "API access")
    raw_key = "bp_" + _secrets.token_hex(20)
    prefix = raw_key[:12]
    hashed = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    row = ApiKey(
        user_id=current_user.id, name=body.name.strip(),
        prefix=prefix, hashed_key=hashed,
    )
    db.add(row); db.commit(); db.refresh(row)
    return {
        "id": str(row.id), "name": row.name, "prefix": row.prefix, "key": raw_key,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "warning": "Save this key now — it will not be shown again.",
    }


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import uuid as _uuid
    _require_feature(current_user, "api_access", "API access")
    try:
        kid = _uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid key id")
    row = db.query(ApiKey).filter(ApiKey.id == kid, ApiKey.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="API key not found")
    if not row.revoked_at:
        row.revoked_at = datetime.utcnow()
        db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOKS (org-scoped via existing WebhookEndpoint model)
# ═══════════════════════════════════════════════════════════════════════════════

ALLOWED_EVENT_TYPES = {
    "report.completed",
    "notarization.anchored",
    "subscription.activated",
    "subscription.canceled",
    "compliance_drift.detected",
    "vendor.health_check.completed",
}


class WebhookCreate(BaseModel):
    url: str = Field(..., min_length=10, max_length=2048)
    events: list[str] = Field(default_factory=list)
    active: bool = True


class WebhookUpdate(BaseModel):
    url: Optional[str] = Field(None, max_length=2048)
    events: Optional[list[str]] = None
    active: Optional[bool] = None


def _validate_events(events: list[str]) -> list[str]:
    cleaned = [e.strip() for e in events if e and e.strip()]
    unknown = [e for e in cleaned if e not in ALLOWED_EVENT_TYPES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown event types: {unknown}")
    return cleaned


def _validate_url(url: str) -> str:
    url = url.strip()
    if not (url.startswith("https://") or url.startswith("http://localhost")):
        raise HTTPException(status_code=400, detail="Webhook URL must use HTTPS (or http://localhost for testing).")
    return url


@router.get("/webhooks")
def list_webhooks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    org = _get_or_create_org(db, current_user)
    rows = (
        db.query(WebhookEndpoint)
        .filter(WebhookEndpoint.organisation_id == org.id)
        .order_by(WebhookEndpoint.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "url": r.url,
                "events": r.events or [],
                "active": bool(r.is_active),
                "signing_secret_prefix": (r.secret or "")[:8] + "…",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "available_events": sorted(ALLOWED_EVENT_TYPES),
    }


@router.post("/webhooks", status_code=201)
def create_webhook(
    body: WebhookCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    org = _get_or_create_org(db, current_user)
    row = WebhookEndpoint(
        organisation_id=org.id,
        url=_validate_url(body.url),
        secret=_secrets.token_hex(32),
        events=_validate_events(body.events),
        is_active=body.active,
    )
    db.add(row); db.commit(); db.refresh(row)
    return {
        "id": str(row.id), "url": row.url, "events": row.events or [],
        "active": row.is_active, "signing_secret": row.secret,
        "warning": "Save this signing secret — it will not be shown again in full.",
    }


def _owned_webhook(db: Session, user: User, wh_id: str) -> WebhookEndpoint:
    import uuid as _uuid
    try:
        uid = _uuid.UUID(wh_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid webhook id")
    org = _get_or_create_org(db, user)
    row = (
        db.query(WebhookEndpoint)
        .filter(WebhookEndpoint.id == uid, WebhookEndpoint.organisation_id == org.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return row


@router.patch("/webhooks/{wh_id}")
def update_webhook(
    wh_id: str, body: WebhookUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    row = _owned_webhook(db, current_user, wh_id)
    if body.url is not None: row.url = _validate_url(body.url)
    if body.events is not None: row.events = _validate_events(body.events)
    if body.active is not None: row.is_active = body.active
    db.commit(); db.refresh(row)
    return {"id": str(row.id), "url": row.url, "active": row.is_active, "events": row.events or []}


@router.delete("/webhooks/{wh_id}", status_code=204)
def delete_webhook(
    wh_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    row = _owned_webhook(db, current_user, wh_id)
    db.delete(row); db.commit()


def _hmac_sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _deliver(db: Session, endpoint: WebhookEndpoint, event_type: str, payload: dict) -> WebhookDelivery:
    body_bytes = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    signature = _hmac_sign(endpoint.secret, body_bytes)
    status_code: Optional[int] = None
    resp_body: Optional[str] = None
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                endpoint.url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Booppa-Event": event_type,
                    "X-Booppa-Signature": f"sha256={signature}",
                    "User-Agent": "Booppa-Webhooks/1.0",
                },
            )
        status_code = resp.status_code
        resp_body = (resp.text or "")[:2048]
    except Exception as e:
        resp_body = f"network_error: {e}"

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id, event_type=event_type, payload=payload,
        status_code=status_code, response_body=resp_body,
        success=(status_code is not None and 200 <= status_code < 300),
    )
    db.add(delivery); db.commit(); db.refresh(delivery)
    return delivery


@router.post("/webhooks/{wh_id}/test")
def test_webhook(
    wh_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    row = _owned_webhook(db, current_user, wh_id)
    delivery = _deliver(db, row, "test.ping", {
        "event": "test.ping",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "user_id": str(current_user.id),
        "message": "If you're reading this, your endpoint is reachable.",
    })
    return {
        "delivered": bool(delivery.success),
        "response_status": delivery.status_code,
        "response_body": delivery.response_body,
    }


@router.get("/webhooks/{wh_id}/deliveries")
def list_deliveries(
    wh_id: str,
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "webhooks", "Webhooks")
    row = _owned_webhook(db, current_user, wh_id)
    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.endpoint_id == row.id)
        .order_by(WebhookDelivery.delivered_at.desc())
        .limit(limit)
        .all()
    )
    return {"items": [
        {
            "id": str(r.id), "event_type": r.event_type,
            "response_status": r.status_code,
            "response_body": (r.response_body or "")[:500],
            "attempt": r.attempt,
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
        }
        for r in rows
    ]}


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-SUBSIDIARY (parent_user_id on users)
# ═══════════════════════════════════════════════════════════════════════════════

class SubsidiaryCreate(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    company: Optional[str] = None
    uen: Optional[str] = None


@router.get("/subsidiaries")
def list_subsidiaries(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "multi_vendor", "Multi-subsidiary management")
    rows = (
        db.query(User).filter(User.parent_user_id == current_user.id)
        .order_by(User.created_at.desc()).all()
    )
    return {"items": [
        {
            "id": str(u.id), "email": u.email, "full_name": u.full_name,
            "company": u.company, "uen": u.uen, "plan": u.plan,
            "is_active": bool(u.is_active),
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in rows
    ]}


@router.post("/subsidiaries", status_code=201)
def add_subsidiary(
    body: SubsidiaryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "multi_vendor", "Multi-subsidiary management")
    if current_user.parent_user_id is not None:
        raise HTTPException(status_code=400, detail="Sub-tenants cannot add subsidiaries.")

    existing = db.query(User).filter(User.email == str(body.email).lower()).first()
    if existing:
        if existing.parent_user_id and existing.parent_user_id != current_user.id:
            raise HTTPException(status_code=409, detail="User already belongs to another tenant.")
        existing.parent_user_id = current_user.id
        if body.company and not existing.company:
            existing.company = body.company
        db.commit(); db.refresh(existing)
        target = existing
    else:
        from app.core.auth import hash_password
        temp_password = _secrets.token_urlsafe(18)
        target = User(
            email=str(body.email).lower(),
            hashed_password=hash_password(temp_password),
            full_name=body.full_name, company=body.company, uen=body.uen,
            role="VENDOR", plan=current_user.plan,
            parent_user_id=current_user.id,
            temp_password=True,
        )
        db.add(target); db.commit(); db.refresh(target)

    return {
        "id": str(target.id), "email": target.email, "company": target.company,
        "parent_user_id": str(current_user.id),
        "needs_invite": bool(getattr(target, "temp_password", False)),
    }


@router.get("/trm/subsidiary-comparison")
def trm_subsidiary_comparison(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pro Suite: roll up MAS TRM control status across the parent tenant and all
    its subsidiaries, so a group CISO sees one consolidated, per-domain view.

    This is the concrete Pro-vs-Standard differentiator (Standard has no
    multi-entity rollup). Each entity reports overall progress, open high/critical
    controls, and a per-domain status map for a side-by-side matrix; we also flag
    subsidiaries materially behind the group leader.
    """
    _require_feature(current_user, "multi_vendor", "Multi-subsidiary TRM comparison")
    if current_user.parent_user_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Only the parent tenant can view the subsidiary comparison.",
        )
    from app.core.models import (
        MAS_TRM_DOMAINS, Organisation, TrmControl,
    )

    entities = [current_user] + (
        db.query(User)
        .filter(User.parent_user_id == current_user.id)
        .order_by(User.created_at.asc())
        .all()
    )
    total_domains = len(MAS_TRM_DOMAINS)

    def _summary(u: User, is_parent: bool) -> dict:
        org = db.query(Organisation).filter(Organisation.owner_user_id == u.id).first()
        by_status = {"not_started": 0, "in_progress": 0, "compliant": 0, "gap": 0}
        domain_status = {d: "not_started" for d in MAS_TRM_DOMAINS}
        critical_open = 0
        last_updated = None
        controls_total = 0
        if org:
            rows = (
                db.query(TrmControl)
                .filter(TrmControl.organisation_id == org.id)
                .all()
            )
            controls_total = len(rows)
            for r in rows:
                st = (r.status or "not_started").lower()
                by_status[st] = by_status.get(st, 0) + 1
                if r.domain in domain_status:
                    domain_status[r.domain] = st
                if (r.risk_rating or "").lower() in ("high", "critical") and st != "compliant":
                    critical_open += 1
                if r.updated_at and (last_updated is None or r.updated_at > last_updated):
                    last_updated = r.updated_at
        denom = controls_total or total_domains
        return {
            "user_id": str(u.id),
            "name": u.company or u.full_name or u.email,
            "is_parent": is_parent,
            "sector": getattr(org, "sector", None) if org else None,
            "domains_complete": by_status["compliant"],
            "domains_total": denom,
            "compliant_pct": round(100 * by_status["compliant"] / denom) if denom else 0,
            "critical_open": critical_open,
            "by_status": by_status,
            "domain_status": domain_status,
            "last_updated": last_updated.isoformat() if last_updated else None,
        }

    summaries = [_summary(u, i == 0) for i, u in enumerate(entities)]

    # Lag alerts — subsidiaries materially behind the group's best performer.
    alerts: list[str] = []
    if len(summaries) > 1:
        best = max(summaries, key=lambda s: s["compliant_pct"])
        for s in summaries:
            if s["user_id"] == best["user_id"]:
                continue
            if best["compliant_pct"] - s["compliant_pct"] >= 30:
                alerts.append(
                    f"{s['name']} is significantly behind {best['name']} on MAS TRM "
                    f"({s['compliant_pct']}% vs {best['compliant_pct']}% complete) — "
                    "risk of inconsistent group-wide MAS response."
                )
            if s["critical_open"] > 0:
                alerts.append(
                    f"{s['name']} has {s['critical_open']} open high/critical control(s)."
                )

    return {
        "entity_count": len(summaries),
        "domains": MAS_TRM_DOMAINS,
        "entities": summaries,
        "alerts": alerts,
    }


@router.delete("/subsidiaries/{sub_id}", status_code=204)
def detach_subsidiary(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import uuid as _uuid
    _require_feature(current_user, "multi_vendor", "Multi-subsidiary management")
    try:
        uid = _uuid.UUID(sub_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid id")
    row = (
        db.query(User)
        .filter(User.id == uid, User.parent_user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Subsidiary not found")
    row.parent_user_id = None
    db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# SSO (org-scoped via existing SsoConfig — verification path stubbed)
# ═══════════════════════════════════════════════════════════════════════════════

class SsoConfigBody(BaseModel):
    protocol: str = Field("saml", pattern="^(saml|oidc)$")
    idp_metadata_url: Optional[str] = None
    idp_metadata_xml: Optional[str] = None
    sp_entity_id: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    allowed_email_domain: Optional[str] = None
    enabled: bool = False


@router.get("/sso")
def get_sso(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "sso", "SSO")
    org = _get_or_create_org(db, current_user)
    row = db.query(SsoConfig).filter(SsoConfig.organisation_id == org.id).first()
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "protocol": row.protocol,
        "idp_metadata_url": row.idp_metadata_url,
        "has_idp_metadata_xml": False,  # existing schema has no xml col
        "sp_entity_id": row.idp_entity_id,
        "oidc_issuer": row.discovery_url,
        "oidc_client_id": row.client_id,
        "has_oidc_client_secret": bool(row.client_secret),
        "allowed_email_domain": None,  # existing schema has no domain col
        "enabled": bool(row.is_active),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.put("/sso")
def put_sso(
    body: SsoConfigBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_feature(current_user, "sso", "SSO")
    org = _get_or_create_org(db, current_user)
    row = db.query(SsoConfig).filter(SsoConfig.organisation_id == org.id).first()
    if not row:
        row = SsoConfig(organisation_id=org.id, protocol=body.protocol)
        db.add(row)
    row.protocol = body.protocol
    row.idp_metadata_url = body.idp_metadata_url
    row.idp_entity_id = body.sp_entity_id
    row.discovery_url = body.oidc_issuer
    row.client_id = body.oidc_client_id
    if body.oidc_client_secret:
        row.client_secret = body.oidc_client_secret
    row.is_active = body.enabled
    db.commit(); db.refresh(row)
    return {"saved": True, "enabled": row.is_active}


@router.post("/sso/acs")
def saml_acs():
    """SAML Assertion Consumer Service — stub.

    Add `python3-saml`, validate the SAMLResponse against the stored IdP
    metadata, then issue a JWT for the matched user. Refusing requests until
    the verification path is implemented so we never accept unsigned assertions.
    """
    raise HTTPException(
        status_code=501,
        detail="SAML ACS endpoint not yet implemented. Install python3-saml and wire up assertion verification before enabling SSO in production.",
    )


@router.get("/sso/oidc/callback")
def oidc_callback():
    """OIDC redirect target — stub. Wire authlib code-exchange flow."""
    raise HTTPException(
        status_code=501,
        detail="OIDC callback not yet implemented. Install authlib and wire up the code-exchange flow before enabling SSO in production.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAS TRM — 13-domain controls + AI gap analysis (user-centric wrapper around
# the org-keyed TrmControl model)
# ═══════════════════════════════════════════════════════════════════════════════

class TrmUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(not_started|in_progress|compliant|gap)$")
    risk_rating: Optional[str] = Field(None, pattern="^(low|medium|high|critical)$")
    gap_analysis: Optional[str] = None


class TrmGapRequest(BaseModel):
    context: str = Field(..., min_length=10, max_length=8000)


def _ensure_trm_controls(db: Session, org_id) -> int:
    """Auto-provision the 13 TRM controls for an org if missing.

    Covers users who subscribed before the webhook fix in 2026-05.
    """
    from app.core.models import TrmControl as _TrmControl
    existing = db.query(_TrmControl).filter(_TrmControl.organisation_id == org_id).count()
    if existing > 0:
        return existing
    from app.trm_workflow_service import initialise_trm_controls
    rows = initialise_trm_controls(str(org_id), db)
    return len(rows)


@router.get("/trm/board-report/latest")
def get_latest_trm_board_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Latest monthly MAS TRM board report for the Suite user, with a freshly
    re-presigned download URL (the stored one expires after 7 days)."""
    from app.core.models import Report

    _require_feature(current_user, "dashboard", "MAS TRM board report")
    row = (
        db.query(Report)
        .filter(
            Report.owner_id == current_user.id,
            Report.framework == "trm_board_report",
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc().nullslast())
        .first()
    )
    if not row:
        return {"available": False}

    ad = row.assessment_data if isinstance(row.assessment_data, dict) else {}
    key = row.file_key or ad.get("s3_key")
    download_url = row.s3_url or ad.get("s3_url")
    if key:
        try:
            from app.services.storage import S3Service
            s3 = S3Service()
            download_url = s3.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3.bucket, "Key": key},
                ExpiresIn=604800,  # 7 days
            )
        except Exception as exc:  # fall back to the stored (maybe-expired) URL
            logger.warning("[TRMBoard] re-presign failed for %s: %s", current_user.id, exc)

    return {
        "available": True,
        "download_url": download_url,
        "generated_at": row.completed_at.isoformat() if row.completed_at else None,
        "compliant_pct": ad.get("compliant_pct"),
        "plan_label": ad.get("plan_label"),
    }


@router.get("/trm/progress-history")
def trm_progress_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """MAS TRM compliance % over time, from the persisted monthly board-report
    snapshots. Surfaces the month-over-month progress that previously lived only
    in the emailed PDF, so the Suite subscriber sees recurring value in-app."""
    from app.core.models import Report

    _require_feature(current_user, "dashboard", "MAS TRM progress history")
    rows = (
        db.query(Report)
        .filter(
            Report.owner_id == current_user.id,
            Report.framework == "trm_board_report",
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc().nullslast())
        .limit(50)
        .all()
    )
    seen_months = set()
    monthly_latest = []
    for r in rows:
        when = r.completed_at or r.created_at
        if not when: continue
        m_key = when.strftime("%Y-%m")
        if m_key not in seen_months:
            seen_months.add(m_key)
            monthly_latest.append(r)
            if len(monthly_latest) == 24:
                break

    points = []
    for r in reversed(monthly_latest):  # oldest → newest
        ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
        pct = ad.get("compliant_pct")
        when = r.completed_at or r.created_at
        if isinstance(pct, (int, float)) and when is not None:
            points.append({
                "label": when.strftime("%b '%y"),
                "compliant_pct": int(round(pct)),
                "generated_at": when.isoformat(),
            })
    return {"points": points}


@router.post("/trm/board-report/generate", status_code=202)
def generate_trm_board_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate this month's MAS TRM board report on demand (also emailed)."""
    _require_feature(current_user, "dashboard", "MAS TRM board report")
    from app.workers.tasks import run_trm_board_report_for_user

    run_trm_board_report_for_user.delay(str(current_user.id))
    return {"status": "queued", "message": "Your board report is being generated and will be emailed shortly."}


@router.get("/trm")
def get_trm(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the user's 13 MAS TRM controls + a roll-up summary."""
    from app.core.models import TrmControl
    # MAS TRM is part of the Standard/Pro Suite enterprise tier — gate on dashboard,
    # which is true for both suites.
    _require_feature(current_user, "dashboard", "MAS TRM dashboard")
    org = _get_or_create_org(db, current_user)
    _ensure_trm_controls(db, org.id)

    rows = (
        db.query(TrmControl)
        .filter(TrmControl.organisation_id == org.id)
        .all()
    )
    # Order by sector criticality so the dashboard leads with the domains a MAS
    # supervisor weights most for this org's sector; with no sector set this is
    # the canonical TRM-1..TRM-13 order.
    from app.services.trm_sector_override import reorder_controls_by_sector
    rows = reorder_controls_by_sector(rows, getattr(org, "sector", None))

    # Evidence counts per control (single grouped query, avoids N+1).
    from app.core.models import TrmEvidence
    from sqlalchemy import func as _func
    _ev_counts = dict(
        db.query(TrmEvidence.control_id, _func.count(TrmEvidence.id))
        .filter(TrmEvidence.control_id.in_([r.id for r in rows] or [None]))
        .group_by(TrmEvidence.control_id)
        .all()
    )

    # Roll-up: 13 statuses → compact summary
    by_status = {"not_started": 0, "in_progress": 0, "compliant": 0, "gap": 0}
    by_risk = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for r in rows:
        s = (r.status or "not_started").lower()
        by_status[s] = by_status.get(s, 0) + 1
        if r.risk_rating:
            by_risk[r.risk_rating.lower()] = by_risk.get(r.risk_rating.lower(), 0) + 1

    total = len(rows) or 1
    return {
        "total": len(rows),
        "summary": {
            "compliant_pct": round(100 * by_status["compliant"] / total),
            "by_status": by_status,
            "by_risk": by_risk,
        },
        "items": [
            {
                "id": str(r.id),
                "domain": r.domain,
                "control_ref": r.control_ref,
                "description": r.description,
                "status": r.status,
                "risk_rating": r.risk_rating,
                "gap_analysis": r.gap_analysis,
                "evidence_count": int(_ev_counts.get(r.id, 0)),
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.patch("/trm/{control_id}")
def update_trm_control(
    control_id: str,
    body: TrmUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import uuid as _uuid
    from app.core.models import TrmControl
    _require_feature(current_user, "dashboard", "MAS TRM dashboard")
    try:
        cid = _uuid.UUID(control_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid control id")
    org = _get_or_create_org(db, current_user)
    ctrl = (
        db.query(TrmControl)
        .filter(TrmControl.id == cid, TrmControl.organisation_id == org.id)
        .first()
    )
    if not ctrl:
        raise HTTPException(status_code=404, detail="Control not found")
    if body.status is not None:
        ctrl.status = body.status
    if body.risk_rating is not None:
        ctrl.risk_rating = body.risk_rating
    if body.gap_analysis is not None:
        ctrl.gap_analysis = body.gap_analysis
    ctrl.updated_at = datetime.utcnow()
    db.commit(); db.refresh(ctrl)
    return {
        "id": str(ctrl.id),
        "domain": ctrl.domain,
        "status": ctrl.status,
        "risk_rating": ctrl.risk_rating,
        "gap_analysis": ctrl.gap_analysis,
        "updated_at": ctrl.updated_at.isoformat() if ctrl.updated_at else None,
    }


@router.post("/trm/{control_id}/gap-analysis")
async def run_trm_gap_analysis(
    control_id: str,
    body: TrmGapRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run Claude haiku-4-5 gap analysis for a single control."""
    import uuid as _uuid
    from app.core.models import TrmControl
    from app.trm_workflow_service import run_gap_analysis
    _require_feature(current_user, "ai_full", "AI gap analysis")
    try:
        cid = _uuid.UUID(control_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid control id")
    org = _get_or_create_org(db, current_user)
    ctrl = (
        db.query(TrmControl)
        .filter(TrmControl.id == cid, TrmControl.organisation_id == org.id)
        .first()
    )
    if not ctrl:
        raise HTTPException(status_code=404, detail="Control not found")
    ctrl = await run_gap_analysis(ctrl, body.context, db)
    return {
        "id": str(ctrl.id),
        "domain": ctrl.domain,
        "status": ctrl.status,
        "risk_rating": ctrl.risk_rating,
        "gap_analysis": ctrl.gap_analysis,
        "updated_at": ctrl.updated_at.isoformat() if ctrl.updated_at else None,
    }


# ── MAS TRM — per-control evidence (upload / list / delete) ────────────────────

_TRM_EVIDENCE_EXTS = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt", ".csv", ".xlsx"}
_TRM_EVIDENCE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB, matches notarize.py


def _owned_trm_control(db: Session, user: User, control_id: str):
    """Resolve a TrmControl scoped to the caller's org, or raise 404/422."""
    import uuid as _uuid
    from app.core.models import TrmControl
    try:
        cid = _uuid.UUID(control_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid control id")
    org = _get_or_create_org(db, user)
    ctrl = (
        db.query(TrmControl)
        .filter(TrmControl.id == cid, TrmControl.organisation_id == org.id)
        .first()
    )
    if not ctrl:
        raise HTTPException(status_code=404, detail="Control not found")
    return org, ctrl


@router.post("/trm/{control_id}/evidence")
async def upload_trm_evidence(
    control_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach an evidence file to a TRM control: validate → S3 → hash → row."""
    from app.core.models import TrmEvidence
    from app.services.storage import S3Service

    _require_feature(current_user, "dashboard", "MAS TRM dashboard")
    org, ctrl = _owned_trm_control(db, current_user, control_id)

    filename = (file.filename or "evidence").replace("/", "-")
    ext = ("." + filename.rsplit(".", 1)[1].lower()) if "." in filename else ""
    if ext not in _TRM_EVIDENCE_EXTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{ext or 'none'}'. Allowed: {', '.join(sorted(_TRM_EVIDENCE_EXTS))}",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file.")
    if len(data) > _TRM_EVIDENCE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")

    file_hash = hashlib.sha256(data).hexdigest()
    s3 = S3Service()
    s3_key = f"trm-evidence/{org.id}/{ctrl.id}/{file_hash[:12]}-{filename}"
    try:
        s3.s3_client.put_object(
            Bucket=s3.bucket, Key=s3_key, Body=data,
            ContentType=file.content_type or "application/octet-stream",
            Metadata={"org-id": str(org.id), "control-id": str(ctrl.id), "file-hash": file_hash},
        )
    except Exception as e:
        logger.error("[TRMEvidence] S3 upload failed for control %s: %s", ctrl.id, e)
        raise HTTPException(status_code=502, detail="Evidence upload failed. Please retry.")

    ev = TrmEvidence(control_id=ctrl.id, file_name=filename, s3_key=s3_key, hash_value=file_hash)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return {
        "id": str(ev.id),
        "file_name": ev.file_name,
        "hash_value": ev.hash_value,
        "uploaded_at": ev.uploaded_at.isoformat() if ev.uploaded_at else None,
    }


@router.get("/trm/{control_id}/evidence")
def list_trm_evidence(
    control_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List evidence files for a control, with short-lived download URLs."""
    from app.core.models import TrmEvidence
    from app.services.storage import S3Service

    _require_feature(current_user, "dashboard", "MAS TRM dashboard")
    _org, ctrl = _owned_trm_control(db, current_user, control_id)

    rows = (
        db.query(TrmEvidence)
        .filter(TrmEvidence.control_id == ctrl.id)
        .order_by(TrmEvidence.uploaded_at.desc())
        .all()
    )
    s3 = S3Service()

    def _url(key: str | None) -> str | None:
        if not key:
            return None
        try:
            return s3.s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": s3.bucket, "Key": key}, ExpiresIn=3600,
            )
        except Exception:
            return None

    return {
        "items": [
            {
                "id": str(r.id),
                "file_name": r.file_name,
                "hash_value": r.hash_value,
                "tx_hash": r.tx_hash,
                "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                "download_url": _url(r.s3_key),
            }
            for r in rows
        ],
    }


@router.delete("/trm/{control_id}/evidence/{evidence_id}")
def delete_trm_evidence(
    control_id: str,
    evidence_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an evidence file (DB row + S3 object)."""
    import uuid as _uuid
    from app.core.models import TrmEvidence
    from app.services.storage import S3Service

    _require_feature(current_user, "dashboard", "MAS TRM dashboard")
    _org, ctrl = _owned_trm_control(db, current_user, control_id)
    try:
        eid = _uuid.UUID(evidence_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid evidence id")

    ev = (
        db.query(TrmEvidence)
        .filter(TrmEvidence.id == eid, TrmEvidence.control_id == ctrl.id)
        .first()
    )
    if not ev:
        raise HTTPException(status_code=404, detail="Evidence not found")

    if ev.s3_key:
        try:
            import asyncio as _asyncio
            _asyncio.run(S3Service().delete_file(ev.s3_key))
        except Exception as e:
            logger.warning("[TRMEvidence] S3 delete failed for %s: %s", ev.s3_key, e)

    db.delete(ev)
    db.commit()
    return {"deleted": True, "id": evidence_id}
