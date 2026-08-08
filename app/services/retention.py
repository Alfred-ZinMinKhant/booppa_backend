"""Enterprise data-retention enforcement.

`RetentionPolicy` rows have been writable and readable since the Enterprise
rollout, but nothing ever acted on them: the policy was a row in a table and the
public privacy page (booppa-nextjs `app/privacy/page.tsx:105`) told customers
"data is securely deleted or anonymized upon expiry of the applicable retention
period". That is exactly the layer-3 failure CLAUDE.md describes — a documented
control with no test evidence behind it. This module is the control.

Two design decisions worth keeping:

1. **The category set is closed.** `RetentionPolicy.data_category` is a
   free-text `String(100)` whose own comment suggests "personal_data" and
   "audit_logs" — neither of which maps to a table that exists. A worker that
   accepted arbitrary strings would either silently no-op (the customer believes
   a purge is running that isn't) or guess at a target table (the customer loses
   data they never nominated). Both are worse than a 422 at write time.

2. **Only org-scoped, time-series data is purgeable.** `Report` is USER-scoped
   (`models.py:105` `owner_id`) with no `organisation_id` at all, so an
   org-level policy has no defensible claim over a member's reports — and those
   reports are anchored evidence the customer paid for. Configuration tables
   (`SsoConfig`, `WhiteLabelConfig`, `WebhookEndpoint`, `Subsidiary`,
   `OrganisationMember`, `TrmControl`) are excluded for a different reason:
   "delete config older than N days" is not retention, it is an outage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app.core.models import (
    OrganisationInvite,
    RetentionPolicy,
    RetentionPurgeLog,
    SlaLog,
    WebhookDelivery,
    WebhookEndpoint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionCategory:
    """One purgeable category: a table, the column that dates a row, and a label."""

    key: str
    label: str
    description: str
    #: Builds the delete-eligible query for one org given a cutoff datetime.
    query: Callable[[Session, str, datetime], object]


def _sla_logs_query(db: Session, org_id: str, cutoff: datetime):
    return db.query(SlaLog).filter(
        SlaLog.organisation_id == org_id,
        SlaLog.recorded_at < cutoff,
    )


def _webhook_deliveries_query(db: Session, org_id: str, cutoff: datetime):
    # WebhookDelivery has no organisation_id of its own — it hangs off the
    # endpoint. Scope through the endpoint rather than purging globally.
    return db.query(WebhookDelivery).filter(
        WebhookDelivery.endpoint_id.in_(
            db.query(WebhookEndpoint.id).filter(WebhookEndpoint.organisation_id == org_id)
        ),
        WebhookDelivery.delivered_at < cutoff,
    )


def _expired_invites_query(db: Session, org_id: str, cutoff: datetime):
    # An ACCEPTED invite is a membership audit record, not a stale artefact:
    # deleting it erases who let a member in. Only unaccepted invites expire.
    return db.query(OrganisationInvite).filter(
        OrganisationInvite.organisation_id == org_id,
        OrganisationInvite.status != "accepted",
        OrganisationInvite.expires_at < cutoff,
    )


RETENTION_CATEGORIES: dict[str, RetentionCategory] = {
    c.key: c
    for c in (
        RetentionCategory(
            key="sla_logs",
            label="SLA logs",
            description="Per-event SLA measurements (target vs actual response time).",
            query=_sla_logs_query,
        ),
        RetentionCategory(
            key="webhook_deliveries",
            label="Webhook delivery logs",
            description=(
                "Outbound webhook attempts, including the request payload and the "
                "receiving system's response body."
            ),
            query=_webhook_deliveries_query,
        ),
        RetentionCategory(
            key="expired_invites",
            label="Expired team invitations",
            description="Team invitations that expired without being accepted.",
            query=_expired_invites_query,
        ),
    )
}


def is_enforceable(category: str) -> bool:
    """True when a stored policy's category maps to a table this module purges.

    Policies predating the closed enum may hold arbitrary strings. They are kept
    (deleting a customer's configuration to tidy our own schema is not our call)
    but must be reported as unenforced so nobody is told a purge is running that
    isn't.
    """
    return category in RETENTION_CATEGORIES


def purge_organisation_policies(
    db: Session,
    policy: RetentionPolicy,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RetentionPurgeLog | None:
    """Enforce ONE policy. Returns the audit row, or None if it was skipped.

    Skips (returning None) when auto_purge is off or the category is not
    enforceable. The caller owns the transaction boundary so that one org's
    failure cannot abort the whole sweep.
    """
    if not policy.auto_purge:
        return None

    category = RETENTION_CATEGORIES.get(policy.data_category)
    if category is None:
        logger.warning(
            "retention: policy %s has unenforceable category %r — skipping",
            policy.id,
            policy.data_category,
        )
        return None

    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=policy.retention_days)
    org_id = str(policy.organisation_id)

    query = category.query(db, org_id, cutoff)
    deleted = query.count()

    if deleted and not dry_run:
        # synchronize_session=False: nothing in this task holds the ORM objects
        # afterwards, and the alternative loads every row into memory first.
        query.delete(synchronize_session=False)

    log = RetentionPurgeLog(
        organisation_id=policy.organisation_id,
        policy_id=policy.id,
        data_category=policy.data_category,
        retention_days=policy.retention_days,
        cutoff=cutoff,
        rows_deleted=deleted,
        dry_run=dry_run,
    )
    db.add(log)

    logger.info(
        "retention: org=%s category=%s cutoff=%s rows=%d%s",
        org_id,
        policy.data_category,
        cutoff.isoformat(),
        deleted,
        " (DRY RUN — nothing deleted)" if dry_run else "",
    )
    return log
