"""Managed client entities for CSP / DPO accounts.

A CSP or DPO manages compliance for many client companies from one login, so the
account's own `User.company` / `User.uen` is never the subject of what it buys.
These endpoints let the customer register each client company once, ACRA-verified
at entry, and then name it explicitly at order time via `managed_entity_id`.

  GET    /managed-entities            → the account's entities
  POST   /managed-entities            → register one (ACRA-verified on create)
  PATCH  /managed-entities/{id}       → edit / archive
  DELETE /managed-entities/{id}       → archive (soft; orders reference these rows)

Identity is written only through `record_verified_identity(carrier=
MANAGED_ENTITY_CARRIER)` — the same single sanctioned write path every other
carrier row uses.
"""
from app.core.route_classes import RetryAPIRoute
import logging

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import verify_access_token
from app.core.db import get_db
from app.core.models import ManagedEntity, User
from app.services.managed_entity_service import (
    UenMismatch,
    find_active_entity,
    resolve_or_create_managed_entity,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RetryAPIRoute)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def _resolve_user(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _serialize(row: ManagedEntity) -> dict:
    return {
        "id": str(row.id),
        "company_name": row.company_name,
        # `legal_name` is what ACRA returned; absent means the entity was accepted
        # without a registry match and its reports must read "Not verified".
        "legal_name": row.legal_name,
        "uen": row.uen,
        "website": row.website,
        "industry": row.industry,
        "verified": bool(row.uen and row.verified_at),
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _owned_or_404(entity_id: str, user: User, db: Session) -> ManagedEntity:
    """Scope every read and write to `owner_user_id`.

    404 rather than 403 on someone else's row: whether a given entity id exists at
    all is not information one customer should be able to probe for another.
    """
    row = (
        db.query(ManagedEntity)
        .filter(ManagedEntity.id == entity_id, ManagedEntity.owner_user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Managed entity not found")
    return row


class ManagedEntityCreate(BaseModel):
    company_name: str
    website: str | None = None
    industry: str | None = None
    uen: str | None = None


class ManagedEntityUpdate(BaseModel):
    company_name: str | None = None
    website: str | None = None
    industry: str | None = None
    status: str | None = None


@router.get("")
def list_entities(
    include_archived: bool = False,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    user = _resolve_user(token, db)
    q = db.query(ManagedEntity).filter(ManagedEntity.owner_user_id == user.id)
    if not include_archived:
        q = q.filter(ManagedEntity.status == "ACTIVE")
    rows = q.order_by(ManagedEntity.company_name.asc()).all()
    return {"items": [_serialize(r) for r in rows]}


@router.post("")
async def create_entity(
    payload: ManagedEntityCreate,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Register a client company, verifying it against ACRA at entry.

    Verifying here rather than at fulfillment is deliberate: a typo caught while the
    customer is looking at the form is a correction; the same typo caught after
    payment is a wrong-entity certificate. A mismatch is a 422, not a silent accept.
    """
    user = _resolve_user(token, db)

    company_name = (payload.company_name or "").strip()
    if not company_name:
        raise HTTPException(status_code=422, detail="company_name is required")

    if find_active_entity(db, user.id, company_name) is not None:
        raise HTTPException(
            status_code=409, detail=f"'{company_name}' is already in your managed entities"
        )

    try:
        result = await resolve_or_create_managed_entity(
            db,
            owner_user_id=user.id,
            company_name=company_name,
            website=payload.website,
            industry=payload.industry,
            claimed_uen=payload.uen,
            # Direct entity registration is the one surface where the customer is
            # looking at the form: a contradicted UEN is a correction to make now,
            # not a row to accept unverified.
            strict=True,
        )
    except UenMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"entity": _serialize(result.entity)}


@router.get("/{entity_id}")
def get_entity(
    entity_id: str,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    user = _resolve_user(token, db)
    return {"entity": _serialize(_owned_or_404(entity_id, user, db))}


@router.patch("/{entity_id}")
def update_entity(
    entity_id: str,
    payload: ManagedEntityUpdate,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Edit the descriptive fields. `uen` / `legal_name` are not editable here.

    Those two are registry facts, not customer-supplied data — the only way they
    change is through `record_verified_identity`. Renaming the entity clears the
    provenance hint, which makes the cached identity untrusted and forces a fresh
    resolve on the next order (the same rule the account rows follow).
    """
    user = _resolve_user(token, db)
    row = _owned_or_404(entity_id, user, db)

    if payload.company_name is not None:
        new_name = payload.company_name.strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="company_name cannot be blank")
        if new_name != row.company_name:
            row.company_name = new_name
            row.verified_hint = None
    if payload.website is not None:
        row.website = payload.website.strip() or None
    if payload.industry is not None:
        row.industry = payload.industry.strip() or None
    if payload.status is not None:
        status = payload.status.strip().upper()
        if status not in {"ACTIVE", "ARCHIVED"}:
            raise HTTPException(status_code=422, detail="status must be ACTIVE or ARCHIVED")
        row.status = status

    db.commit()
    db.refresh(row)
    return {"entity": _serialize(row)}


@router.delete("/{entity_id}")
def archive_entity(
    entity_id: str,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Archive rather than delete — past orders reference this row as their subject,
    and a delivered report must stay able to say which entity it was about."""
    user = _resolve_user(token, db)
    row = _owned_or_404(entity_id, user, db)
    row.status = "ARCHIVED"
    db.commit()
    return {"success": True, "entity": _serialize(row)}
