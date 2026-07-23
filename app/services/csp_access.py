"""CSP organisation provisioning + access activation.

Shared by the CSP router (`app/api/csp.py`) and the Stripe webhook
(`app/api/stripe_webhook.py`). Kept in `app/services/` rather than the router so
the webhook can import it without pulling the FastAPI router (and its auth
dependencies) into the webhook import graph.

Access model: every authenticated user gets a CspOrganisation row on first touch
(so `org_id` always resolves), but it starts `subscription_status="inactive"`.
The router gates all endpoints with HTTP 402 until a paid Stripe purchase calls
`activate_csp_access`, which flips the org to active.

Fulfillment call sites should use `deliver_csp_activation` rather than
`activate_csp_access` directly — it also queues the Day-1 deliverable, and it is
the single place both the one-time and monthly purchase paths share.
"""
from __future__ import annotations

import logging

from app.core.config import settings
from app.core.models import CspOrganisation, CspOrgMembership

logger = logging.getLogger(__name__)


def find_or_create_csp_org(db, user) -> CspOrganisation:
    """Return the user's CSP organisation, creating it (inactive) on first use.

    Creates the org + a `csp_admin` membership. Idempotent: returns the existing
    org when a membership already exists. Does NOT grant access — see
    `activate_csp_access`.
    """
    membership = (
        db.query(CspOrgMembership)
        .filter(CspOrgMembership.user_id == user.id)
        .order_by(CspOrgMembership.created_at.asc())
        .first()
    )
    if membership:
        return membership.organisation

    org = CspOrganisation(
        name=(getattr(user, "company", None) or user.email or "CSP Organisation"),
        owner_user_id=user.id,
        plan="full",
        monthly_fee_sgd=float(getattr(settings, "CSP_MONTHLY_FEE_SGD", 299.0)),
        subscription_status="inactive",
    )
    db.add(org)
    db.flush()
    db.add(CspOrgMembership(org_id=org.id, user_id=user.id, role="csp_admin"))
    db.commit()
    db.refresh(org)
    return org


def activate_csp_access(
    db,
    *,
    user,
    plan: str,
    billing_type: str,
    monthly_fee_sgd: float = 299.0,
) -> CspOrganisation:
    """Mark the user's CSP organisation active after a paid Stripe purchase.

    Idempotent — safe to call on webhook replays. `billing_type` is
    "subscription" or "one_time"; `plan` is "csp" (full) or "csp_monitoring".
    The liability cap shown at ToS acceptance is `monthly_fee_sgd * 12`, so we
    keep the monthly fee at S$299 for all tiers (incl. one-time) for a
    consistent S$3,588 cap.
    """
    org = find_or_create_csp_org(db, user)
    org.subscription_status = "active"
    org.billing_type = billing_type
    org.plan = plan
    if monthly_fee_sgd:
        org.monthly_fee_sgd = float(monthly_fee_sgd)
    db.commit()
    db.refresh(org)
    return org


async def deliver_csp_activation(
    db,
    *,
    user,
    plan: str,
    billing_type: str,
    metadata: dict | None = None,
    session_id: str | None = None,
    test_simulation: bool = False,
) -> CspOrganisation:
    """Activate CSP access AND queue the Day-1 deliverable. Single entry point.

    Both purchase paths — the one-time pack (`fulfillment/bundles.py`) and the
    monthly subscription (`fulfillment/subscriptions.py`) — call this. They used
    to each activate the org and send a bare two-line activation email with
    nothing attached: one gap hit from two call sites. Fixing it here rather than
    at either call site keeps it one fix, not two.

    The 8 AML/CFT documents still (correctly) wait for the CSP to submit a
    profile — an AML/CFT programme can't be written before we know the firm's
    business. What ships now is `csp.run_baseline`: an ACRA-verified entity
    baseline plus an honest initialised/outstanding structure, emailed as ONE
    message that also serves as the activation notice.

    Async by design: both call sites already run inside `asyncio.run()` in the
    Celery worker, where a sync helper that bridged via `asyncio.run()` itself
    would silently no-op.
    """
    org = activate_csp_access(db, user=user, plan=plan, billing_type=billing_type)

    meta = metadata or {}
    try:
        from app.workers.csp_tasks import run_csp_baseline_for_user

        run_csp_baseline_for_user.apply_async(
            kwargs={
                "user_id": str(user.id),
                "plan": plan,
                "billing_type": billing_type,
                "override_company": (meta.get("company_name") or "").strip() or None,
                "override_website": (
                    meta.get("vendor_url") or meta.get("website_url") or ""
                ).strip() or None,
                # The admin simulate-purchase harness re-runs the same purchase
                # for the same user; the 24h once-only send lock would otherwise
                # swallow every run after the first.
                "bypass_idempotency": bool(test_simulation),
            },
            countdown=3,
        )
        logger.info(
            "[CSP] Activation delivered for %s (plan=%s billing=%s session=%s); "
            "baseline queued",
            user.email, plan, billing_type, session_id,
        )
    except Exception as exc:
        # Access is already granted and committed — a broker hiccup must not undo
        # a paid activation. Alert so the baseline can be re-queued by hand.
        logger.error(
            "[CSP] baseline queue failed for %s (session=%s): %s",
            user.email, session_id, exc,
        )
        try:
            from app.services.fulfillment.helpers import (
                _alert_payment_fulfillment_issue,
            )

            await _alert_payment_fulfillment_issue(
                reason="CSP access activated but Day-1 baseline task failed to queue",
                product_type=f"csp:{plan}",
                customer_email=user.email,
                session_id=session_id,
            )
        except Exception:
            logger.exception("[CSP] alert for failed baseline queue also failed")

    return org
