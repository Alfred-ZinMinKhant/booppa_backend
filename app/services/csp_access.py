"""CSP organisation provisioning + access activation.

Shared by the CSP router (`app/api/csp.py`) and the Stripe webhook
(`app/api/stripe_webhook.py`). Kept in `app/services/` rather than the router so
the webhook can import it without pulling the FastAPI router (and its auth
dependencies) into the webhook import graph.

Access model: every authenticated user gets a CspOrganisation row on first touch
(so `org_id` always resolves), but it starts `subscription_status="inactive"`.
The router gates all endpoints with HTTP 402 until a paid Stripe purchase calls
`activate_csp_access`, which flips the org to active.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.models_csp import CspOrganisation, CspOrgMembership


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
