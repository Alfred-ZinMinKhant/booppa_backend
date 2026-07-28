"""Register / resolve a customer-owned `ManagedEntity`, ACRA-verified at entry.

Extracted from `app/api/managed_entities.py:create_entity` so that every surface
which names a client company routes through one implementation:

  * `POST /managed-entities`      — the CSP/DPO adds a client directly
  * `POST /csp/clients`           — the AML/CDD case file names its subject
  * admin test-checkout           — QA names a subject without pre-seeding rows

Before this existed, `csp_clients.uen_or_reg_no` was free text that no identity
resolver ever read, so a UEN typed into a bulk-import CSV could reach a customer
document without ever having been checked against the registry. That is the same
class of failure as the SPQR leak — an unverified UEN is a real UEN belonging to
*someone*, and `fetch_acra_status` will happily return that someone's record.

Identity columns are written only through `record_verified_identity(carrier=
MANAGED_ENTITY_CARRIER)`. This module never assigns `uen` / `legal_name` itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.models import ManagedEntity
from app.services.evidence_enricher import (
    MANAGED_ENTITY_CARRIER,
    fetch_acra_status,
    record_verified_identity,
)

logger = logging.getLogger(__name__)


class UenMismatch(ValueError):
    """The caller claimed a UEN that ACRA attributes to a different company.

    Raised only when `strict=True`. Callers that cannot reject the row outright
    (an AML case file must still be creatable for a mistyped client) pass
    `strict=False` and get an unverified entity plus `mismatch` set instead.
    """


@dataclass
class EntityResolution:
    entity: ManagedEntity
    #: False when an ACTIVE row for this owner+name already existed.
    created: bool
    #: True only on a confirmed registry match — drives "Not verified" downstream.
    verified: bool
    #: Human-readable explanation when a claimed UEN contradicted the registry.
    mismatch: Optional[str] = None


def find_active_entity(
    db: Session, owner_user_id, company_name: str
) -> Optional[ManagedEntity]:
    """The owner's ACTIVE entity with this exact name, if any."""
    return (
        db.query(ManagedEntity)
        .filter(
            ManagedEntity.owner_user_id == owner_user_id,
            ManagedEntity.company_name == company_name,
            ManagedEntity.status == "ACTIVE",
        )
        .first()
    )


async def resolve_or_create_managed_entity(
    db: Session,
    *,
    owner_user_id: UUID,
    company_name: str,
    website: Optional[str] = None,
    industry: Optional[str] = None,
    claimed_uen: Optional[str] = None,
    strict: bool = True,
    commit: bool = True,
) -> EntityResolution:
    """Return the owner's entity for `company_name`, creating and verifying it if new.

    An existing ACTIVE row is returned as-is (`created=False`) and is **not**
    re-verified here: its `verified_hint` provenance already governs whether its
    cached identity may be trusted, and re-resolving on every call would turn a
    registry outage into a silent downgrade of a previously-verified entity.

    A new row is created first and only then written through
    `record_verified_identity`, so an unmatched company persists with NULL
    `uen`/`legal_name` — the state every downstream reader renders "Not verified".
    """
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("company_name is required")

    website = (website or "").strip() or None
    industry = (industry or "").strip() or None
    claimed_uen = (claimed_uen or "").strip() or None

    existing = find_active_entity(db, owner_user_id, company_name)
    if existing is not None:
        return EntityResolution(
            entity=existing,
            created=False,
            verified=bool(existing.uen and existing.verified_at),
        )

    try:
        acra = await fetch_acra_status(company_name=company_name, uen=claimed_uen)
    except Exception as exc:
        # A registry outage must not block the customer from setting up their
        # account. The row is created unverified and re-resolves at order time.
        logger.warning(
            "resolve_or_create_managed_entity: ACRA lookup failed for %r: %s",
            company_name, exc,
        )
        acra = None

    matched = bool(acra and acra.get("found"))
    resolved_uen = (acra or {}).get("uen") if matched else None
    registered_name = (acra or {}).get("registered_name") if matched else None

    mismatch: Optional[str] = None
    if (
        claimed_uen
        and matched
        and resolved_uen
        and claimed_uen.upper() != str(resolved_uen).upper()
    ):
        mismatch = (
            f"UEN {claimed_uen} does not match ACRA's record for '{company_name}' "
            f"({resolved_uen}). Check which is correct before adding this entity."
        )
        if strict:
            raise UenMismatch(mismatch)
        # Non-strict: the row is still created, but a contradicted UEN is never
        # grounds to stamp identity — drop the match entirely rather than picking
        # a winner between two claims about who this company is.
        matched = False
        resolved_uen = None
        registered_name = None

    row = ManagedEntity(
        owner_user_id=owner_user_id,
        company_name=company_name,
        website=website,
        industry=industry,
        status="ACTIVE",
    )
    db.add(row)
    db.flush()

    verified = False
    if matched and (resolved_uen or registered_name):
        verified = record_verified_identity(
            row,
            db,
            uen=resolved_uen,
            legal_name=registered_name,
            company_hint=company_name,
            website_hint=website,
            carrier=MANAGED_ENTITY_CARRIER,
        )

    if commit:
        db.commit()
        db.refresh(row)

    return EntityResolution(
        entity=row, created=True, verified=verified, mismatch=mismatch
    )
