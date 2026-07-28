"""Backfill `csp_clients.managed_entity_id` for rows created before the link existed.

Run once after `alembic upgrade 2026_07_28_0003`:

    python -m scripts.backfill_csp_client_entities --dry-run
    python -m scripts.backfill_csp_client_entities

WHY THIS DOES NOT VERIFY ANYTHING

Every entity created here lands UNVERIFIED — NULL `uen`, NULL `legal_name`, NULL
`verified_hint`. That is deliberate and is the entire point of the exercise.

The pre-existing `csp_clients.uen_or_reg_no` values are free text: typed into a
form or pasted from a bulk-import CSV, never checked against the registry. Copying
them into `ManagedEntity.uen` would launder unverified data into the one table
whose contract is "this identity was confirmed", and a wrong-but-real UEN resolves
to a real company — the SPQR failure shape exactly.

Each entity re-verifies for real on first use, through
`resolve_managed_entity_identity`. Until then its reports honestly read
"Not verified".

Individuals (`client_type='individual'`) are skipped: a natural person has no
company identity.
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.core.db import SessionLocal
from app.core.models import CspClient, CspOrgMembership, CspProfile, ManagedEntity

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")


def _owner_user_id(db, client: CspClient):
    """The user who owns the CSP org this client belongs to.

    `ManagedEntity` is keyed on `owner_user_id` while `CspClient` hangs off
    `csp_profiles` → `csp_organisations`, so the ownership chain has to be walked.
    Where an org has several members we take the earliest membership: the entity is
    owned by the firm, and any member resolves to the same rows at order time.
    """
    profile = db.query(CspProfile).filter(CspProfile.id == client.csp_id).first()
    if profile is None:
        return None
    membership = (
        db.query(CspOrgMembership)
        .filter(CspOrgMembership.org_id == profile.organisation_id)
        .order_by(CspOrgMembership.id.asc())
        .first()
    )
    return membership.user_id if membership else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    db = SessionLocal()
    linked = created = skipped_individual = unresolved_owner = 0
    try:
        clients = (
            db.query(CspClient).filter(CspClient.managed_entity_id.is_(None)).all()
        )
        logger.info("%d csp_clients rows without a linked entity", len(clients))

        for client in clients:
            if (client.client_type or "").lower() == "individual":
                skipped_individual += 1
                continue

            name = (client.legal_name or "").strip()
            if not name:
                continue

            owner_id = _owner_user_id(db, client)
            if owner_id is None:
                unresolved_owner += 1
                logger.warning("  no owner for client %s (%r) — skipped", client.id, name)
                continue

            entity = (
                db.query(ManagedEntity)
                .filter(
                    ManagedEntity.owner_user_id == owner_id,
                    ManagedEntity.company_name == name,
                    ManagedEntity.status == "ACTIVE",
                )
                .first()
            )
            if entity is None:
                if args.dry_run:
                    logger.info("  would create entity %r for owner %s", name, owner_id)
                    created += 1
                    continue
                # Unverified by construction — see the module docstring.
                entity = ManagedEntity(
                    owner_user_id=owner_id, company_name=name, status="ACTIVE"
                )
                db.add(entity)
                db.flush()
                created += 1

            if args.dry_run:
                logger.info("  would link client %s → entity %r", client.id, name)
            else:
                client.managed_entity_id = entity.id
            linked += 1

        if args.dry_run:
            db.rollback()
            logger.info("DRY RUN — nothing written")
        else:
            db.commit()

        logger.info(
            "linked=%d created_entities=%d skipped_individual=%d unresolved_owner=%d",
            linked, created, skipped_individual, unresolved_owner,
        )
        return 0
    except Exception:
        db.rollback()
        logger.exception("backfill failed — rolled back")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
