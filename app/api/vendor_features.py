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
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User
from app.core.models_v12 import ApiKey
from app.core.models_enterprise import (
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
    org = Organisation(
        name=user.company or user.full_name or user.email,
        slug=slug,
        tier=tier,
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
