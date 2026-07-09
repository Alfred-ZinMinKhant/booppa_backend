from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service

from app.services.fulfillment.helpers import (
    _log_purchase_activity,
    _apply_subscription_score_lever,

    _create_stub_report,
    _alert_payment_fulfillment_issue,
    _maybe_fire_cover_sheet,
    _fire_strategy_6,
)
from app.services.fulfillment.single_products import _defer_rfp_to_intake

from app.services.email_service import EmailService
from app.billing.enforcement import enforce_tier
from app.core.models import Referral
from datetime import datetime, timedelta, timezone
import stripe
import logging
import json
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

router = APIRouter()


RFP_PRODUCT_TYPES = {"rfp_express", "rfp_complete"}
# Single-document notarization is one-time (pay-per-doc, grants a credit balance).
# The 10/50 batch tiers are now subscriptions (monthly quota) — see
# SUBSCRIPTION_PRODUCT_TYPES + ENTERPRISE_NOTARIZATION_LIMITS.
NOTARIZATION_PRODUCT_TYPES = {
    "compliance_notarization_1",
    "notarization_addon_1",
}
NOTARIZATION_CREDIT_AMOUNTS = {
    "compliance_notarization_1": 1,
    "notarization_addon_1": 1,
}
VENDOR_PROOF_PRODUCT_TYPES = {"vendor_proof"}
PDPA_PRODUCT_TYPES = {"pdpa_quick_scan", "pdpa_snapshot"}
SUBSCRIPTION_PRODUCT_TYPES = {
    "vendor_active_monthly",
    "vendor_active_annual",
    "pdpa_monitor_monthly",
    "pdpa_monitor_annual",
    "enterprise_monthly",
    "enterprise_pro_monthly",
    "standard_suite_monthly",
    "pro_suite_monthly",
    "evaluate_suppliers_monthly",
    "verify_supplier_evidence_monthly",
    "compliance_evidence_monthly",
    "tender_intelligence_monthly",
    "tender_intelligence_annual",
    "vendor_pro_monthly",
    "vendor_pro_annual",
    # Buyer ladder
    "buyer_starter_monthly",
    "buyer_starter_annual",
    "buyer_pro_monthly",
    "buyer_pro_annual",
    "buyer_enterprise_monthly",
    "buyer_enterprise_annual",
    # Batch notarization tiers are recurring monthly allowances.
    "compliance_notarization_10",
    "compliance_notarization_50",
    # CSP Compliance Pack recurring tiers (one-time grant handled separately).
    "csp_pack_monthly",
    "csp_monitoring_monthly",
}

# CSP one-time pack purchase — grants lifetime pack access (no recurring billing).
CSP_ONETIME_PRODUCT_TYPES = {"csp_pack_onetime"}

# Bundle → component mapping.
# Each bundle fans out to multiple fulfillment tasks.
# notarization_count = how many notarization tasks to queue (each for one document credit).
BUNDLE_COMPONENTS = {
    "vendor_trust_pack": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": None,
    },
    "rfp_accelerator": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": "rfp_express",
    },
    "enterprise_bid_kit": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 7,  # 2 from Trust Pack + 5 additional
        "rfp": "rfp_complete",
    },
    "compliance_evidence_pack": {
        "vendor_proof": False,
        "pdpa": True,
        "notarization_count": 1,
        "rfp": "rfp_complete",
        "cover_sheet": True,  # triggers cover sheet generation with 300s delay
    },
}

# Grace window after which the Compliance Evidence Pack cover sheet fires with
# PDPA + RFP only, when the buyer never completed the BCEP evidence-pack intake
# (so the 7-doc pack never reaches status="ready"). Keeps a buyer from being
# left without any cover sheet. See `_maybe_fire_cover_sheet`.
_COVER_SHEET_BCEP_GRACE_DAYS = 7


# Subscription tier → VerifyRecord.verification_level mapping.
# Paid plans elevate the compliance multiplier (BASIC 1.0× → STANDARD 1.1× →
# PREMIUM 1.3× → GOVERNMENT 1.5×, see scoring.py:92). The mapping is conservative
# on purpose — enterprise_pro is the only plan that grants GOVERNMENT tier.
_PLAN_TO_VERIFICATION_LEVEL = {
    "vendor_active": "STANDARD",
    "pdpa_monitor": "STANDARD",
    "evaluate_suppliers": "STANDARD",
    "standard_suite": "STANDARD",
    "tender_intelligence": "STANDARD",
    "vendor_pro": "STANDARD",
    "enterprise": "PREMIUM",
    "pro_suite": "PREMIUM",
    "verify_supplier_evidence": "PREMIUM",
    "compliance_evidence": "PREMIUM",
    "enterprise_pro": "GOVERNMENT",
    # Buyer ladder — mirrors evaluate_suppliers / verify_supplier_evidence.
    # Note: buyer-side plans don't elevate the holder's own vendor verification
    # (most holders are buyers, not vendors) but the mapping is needed so
    # the score-lever code path is a no-op rather than a KeyError.
    "buyer_starter": "STANDARD",
    "buyer_pro": "STANDARD",
    "buyer_enterprise": "PREMIUM",
}
_LEVEL_RANK = {"BASIC": 0, "STANDARD": 1, "PREMIUM": 2, "GOVERNMENT": 3}




def _csp_activation_email_html(plan: str) -> str:
    """Plain activation email for a CSP Compliance Pack purchase."""
    label = "CSP Monitoring Add-On" if plan == "csp_monitoring" else "CSP Compliance Pack — Full"
    return (
        f"<p>Your <strong>{label}</strong> is now active.</p>"
        "<p>Sign in and open the CSP Compliance dashboard to accept the Terms of "
        "Service, set up your CSP profile, and start onboarding clients with full "
        "AML/CFT, CDD/EDD, sanctions screening, and blockchain-notarized records.</p>"
        "<p>— The Booppa Team</p>"
    )


async def _activate_subscription(
    product_type: str,
    customer_email: str | None,
    stripe_subscription_id: str | None,
    stripe_customer_id: str | None,
    test_simulation: bool = False,
    override_company: str | None = None,
    override_website: str | None = None,
    demo: bool = False,
) -> None:
    """
    Persist subscription state when a new Stripe subscription is created or renewed.
    Grants the appropriate platform role/plan to the user.

    `test_simulation` is set by the admin simulate-purchase harness; it propagates
    into any auto-fulfilled bundle so RFP-bearing tiers (e.g. compliance_evidence)
    skip the brief intake and generate the kit directly — matching the standalone
    bundle test path in `_fulfill_bundle`.

    `override_company` / `override_website` are ALSO test-harness-only: the admin
    Test Identity supplies the company + website that first-cycle deliverables
    (Vendor snapshot, PDPA Monitor report) should reflect, WITHOUT mutating the
    real user profile (the harness email can be a real account). They are passed
    through to the per-user first-cycle wrappers; production renewals leave them
    None and fall back to the stored profile as before.

    `demo` is set True ONLY when the originating Stripe event's `livemode` is
    explicitly False (test-mode checkout). It routes buyer activations to the
    `buyer_demo_fireall_task` preview fan-out instead of the single first-cycle
    digest, so a client can see every buyer email in one activation. It must never
    be True for a real live buyer.
    """
    db = SessionLocal()
    try:
        from app.core.models import User

        user = None
        if customer_email:
            user = db.query(User).filter(User.email == customer_email).first()

        if not user:
            await _alert_payment_fulfillment_issue(
                reason="subscription paid but no user row matched customer_email",
                product_type=product_type,
                customer_email=customer_email,
                session_id=stripe_subscription_id,
                extra={"stripe_customer_id": stripe_customer_id},
            )
            return

        # Map product_type → platform plan
        plan_map = {
            "vendor_active_monthly": "vendor_active",
            "vendor_active_annual": "vendor_active",
            "pdpa_monitor_monthly": "pdpa_monitor",
            "pdpa_monitor_annual": "pdpa_monitor",
            "enterprise_monthly": "enterprise",
            "enterprise_pro_monthly": "enterprise_pro",
            "standard_suite_monthly": "standard_suite",
            "pro_suite_monthly": "pro_suite",
            "evaluate_suppliers_monthly": "evaluate_suppliers",
            "verify_supplier_evidence_monthly": "verify_supplier_evidence",
            "compliance_evidence_monthly": "compliance_evidence",
            "tender_intelligence_monthly": "tender_intelligence",
            "tender_intelligence_annual": "tender_intelligence",
            "vendor_pro_monthly": "vendor_pro",
            "vendor_pro_annual": "vendor_pro",
            # Buyer ladder. Monthly + annual share the same plan family so
            # cancellation/upgrade flows treat them as one.
            "buyer_starter_monthly": "buyer_starter",
            "buyer_starter_annual": "buyer_starter",
            "buyer_pro_monthly": "buyer_pro",
            "buyer_pro_annual": "buyer_pro",
            "buyer_enterprise_monthly": "buyer_enterprise",
            "buyer_enterprise_annual": "buyer_enterprise",
            # Batch notarization subscriptions keep their slug as the plan.
            "compliance_notarization_10": "compliance_notarization_10",
            "compliance_notarization_50": "compliance_notarization_50",
            # CSP Compliance Pack.
            "csp_pack_monthly": "csp",
            "csp_monitoring_monthly": "csp_monitoring",
        }
        new_plan = plan_map.get(product_type, "pro")

        # ── CSP Compliance Pack ─────────────────────────────────────────────
        # CSP is a separate product axis tracked on the CspOrganisation, NOT on
        # user.plan (overwriting that would clobber a vendor/buyer's platform
        # plan if they also buy CSP). Activate the org and return early, before
        # the user.plan assignment + platform feature triggers below.
        if product_type in ("csp_pack_monthly", "csp_monitoring_monthly"):
            from app.services.csp_access import activate_csp_access

            if stripe_subscription_id:
                user.stripe_subscription_id = stripe_subscription_id
            if stripe_customer_id:
                user.stripe_customer_id = stripe_customer_id
            activate_csp_access(
                db, user=user, plan=new_plan, billing_type="subscription"
            )
            logger.info(
                f"[CSP] Activated {new_plan} access for {customer_email}"
            )
            try:
                sent = await EmailService().send_html_email(
                    user.email,
                    "Your CSP Compliance Pack is active",
                    _csp_activation_email_html(new_plan),
                )
                if not sent:
                    await _alert_payment_fulfillment_issue(
                        reason="CSP activated but activation email rejected by provider",
                        product_type=product_type,
                        customer_email=customer_email,
                        session_id=stripe_subscription_id,
                    )
            except Exception as e:
                logger.warning(f"[CSP] activation email failed: {e}")
            return

        user.plan = new_plan
        user.subscription_tier = new_plan
        try:
            from datetime import datetime, timezone as _tz

            _now = datetime.now(_tz.utc)
            user.subscription_started_at = _now
            # Stored uncapped (1-31). The daily cron filter matches:
            #   • anniversary == today.day on a regular day, OR
            #   • anniversary >= today.day on the last day of a short month
            # so a Jan-31 subscriber gets their cycle on Feb 28, Apr 30, etc.
            user.subscription_anniversary_day = _now.day
        except Exception:
            pass
        if stripe_subscription_id:
            user.stripe_subscription_id = stripe_subscription_id
        if stripe_customer_id:
            user.stripe_customer_id = stripe_customer_id
        db.commit()

        # ── Once-only side-effect guard ─────────────────────────────────────
        # `_activate_subscription` is reachable from BOTH the synchronous
        # `checkout.session.completed` handler AND the async
        # `customer.subscription.created` handler (plus webhook replays). The
        # entitlement writes above are idempotent, but the activation email and
        # the first-cycle `.delay()` fan-out below are NOT — running them twice
        # double-emails the buyer and double-queues their first deliverable.
        # Claim a once-per-subscription slot atomically (Redis SET NX) so only
        # the first caller fires side effects. Absent a subscription id (should
        # not happen for subs) we fall open and treat it as the first run.
        first_activation = True
        if stripe_subscription_id:
            try:
                from app.core.cache import cache as _cache
                from datetime import datetime, timezone as _tz2

                first_activation = _cache.add(
                    _cache.cache_key(f"sub_activated:{stripe_subscription_id}"),
                    {"activated_at": datetime.now(_tz2.utc).isoformat()},
                    ttl=86400,  # 24h: long enough to absorb retry storms /
                                # dual-event delivery, short enough that a genuine
                                # re-subscribe next cycle still re-activates.
                )
            except Exception as guard_err:
                logger.warning(
                    f"[Subscription] Activation guard check failed (firing once anyway): {guard_err}"
                )
                first_activation = True
        if not first_activation:
            logger.info(
                f"[Subscription] Skipping duplicate activation side-effects for "
                f"sub={stripe_subscription_id} ({new_plan}) — already activated"
            )

        # ── Instant first-cycle delivery ────────────────────────────────────
        # Subscribers shouldn't wait up to 30 days for their first deliverable.
        # Each tier fires the same task its monthly cron would fire, scoped to
        # just this user. All async via .delay() so checkout webhook returns
        # quickly; any failure surfaces in worker logs without blocking the
        # entitlement grant above. Gated on `first_activation` so a dual webhook
        # delivery doesn't queue the first cycle twice.
        try:
            from app.workers import tasks as _wtasks
            if not first_activation:
                pass
            elif new_plan == "tender_intelligence":
                # Sector digest — not company/website-specific, no override needed.
                _wtasks.send_tender_intelligence_digest_for_user.delay(str(user.id))
            elif new_plan == "pdpa_monitor":
                _wtasks.run_pdpa_monitor_cycle_for_user.delay(
                    str(user.id),
                    override_website=override_website,
                    override_company=override_company,
                )
            elif new_plan == "compliance_evidence":
                _wtasks.run_compliance_evidence_cycle_for_user.delay(
                    str(user.id), test_simulation=test_simulation,
                    override_website=override_website,
                    override_company=override_company,
                )
            elif new_plan == "vendor_active":
                _wtasks.run_vendor_active_check_for_user.delay(
                    str(user.id), override_company=override_company,
                )
            elif new_plan == "vendor_pro":
                _wtasks.run_vendor_pro_activation_for_user.delay(
                    str(user.id),
                    override_website=override_website,
                    override_company=override_company,
                )
            elif new_plan in ("buyer_starter", "buyer_pro", "buyer_enterprise"):
                if demo:
                    # Test-mode checkout (Stripe livemode=false): fire EVERY buyer
                    # deliverable to this inbox, [DEMO]-tagged, mock hash (no gas), so
                    # the client sees the full proactive email set in one activation.
                    # NEVER reached for a real live buyer — `demo` is only True when
                    # the webhook event's livemode is explicitly False.
                    _wtasks.buyer_demo_fireall_task.delay(
                        str(user.id),
                        user.email,
                        product_type=product_type,
                        override_company=override_company,
                    )
                else:
                    # Procurement Intelligence Digest — first-cycle/welcome mode.
                    # Starter = email summary; Pro/Enterprise = + attached
                    # Procurement Report PDF. Tier is resolved inside the task from
                    # the raw product_type SKU.
                    _wtasks.buyer_procurement_digest_task.delay(
                        str(user.id),
                        user.email,
                        product_type=product_type,
                        override_company=override_company,
                        is_first_cycle=True,
                    )
            elif new_plan in ("standard_suite", "pro_suite"):
                # Deliver the MAS TRM Baseline Assessment PDF. Small countdown so
                # the TRM controls initialised later in this same activation are
                # committed first (the task also falls back to canonical domains).
                _wtasks.run_suite_trm_baseline_for_user.apply_async(
                    args=[str(user.id)],
                    kwargs={"override_company": override_company},
                    countdown=30,
                )
            logger.info(
                "[Subscription] First-cycle delivery queued for user=%s tier=%s",
                user.email, new_plan,
            )
        except Exception as e:
            logger.warning(
                "[Subscription] First-cycle delivery failed to enqueue for user=%s tier=%s: %s",
                user.email, new_plan, e,
            )

        # Refresh seat caps on every org this user owns so plan upgrades take
        # effect immediately (e.g. Starter -> Pro bumps max_seats 1 -> 3).
        # Downgrades do NOT retroactively evict members — they just block new
        # invites until seats free up.
        try:
            from app.billing.enforcement import max_seats_for
            from app.core.models import Organisation as _Org

            new_cap = max_seats_for(new_plan)
            owned_orgs = db.query(_Org).filter(_Org.owner_user_id == user.id).all()
            for _org in owned_orgs:
                _org.max_seats = new_cap
            if owned_orgs:
                db.commit()
                logger.info(
                    f"[Subscription] Updated max_seats={new_cap} on {len(owned_orgs)} org(s) for {customer_email}"
                )
        except Exception as seat_err:
            logger.warning(
                f"[Subscription] Failed to refresh org seat caps: {seat_err}"
            )

        # Upsert the Subscription table row so it's the source of truth for
        # multi-subscription support (a user can have vendor_active + pdpa_monitor).
        if stripe_subscription_id:
            try:
                from app.core.models import Subscription as SubModel

                existing = (
                    db.query(SubModel)
                    .filter(SubModel.stripe_subscription_id == stripe_subscription_id)
                    .first()
                )
                if existing:
                    existing.status = "active"
                    existing.product_type = product_type
                    existing.stripe_customer_id = stripe_customer_id
                else:
                    db.add(
                        SubModel(
                            user_id=user.id,
                            stripe_subscription_id=stripe_subscription_id,
                            stripe_customer_id=stripe_customer_id,
                            product_type=product_type,
                            status="active",
                        )
                    )
                db.commit()
            except Exception as sub_err:
                logger.warning(
                    f"[Subscription] Subscription table upsert failed: {sub_err}"
                )

        logger.info(
            f"[Subscription] Activated plan={new_plan} for user={customer_email}"
        )

        # Send confirmation email. Suites and buyer tiers get a richer, itemised
        # onboarding email instead (sent from their provisioning blocks below).
        # vendor_active / vendor_pro are ALSO excluded: their first-cycle health
        # check sends ONE consolidated welcome digest (snapshot + GeBIZ alerts +
        # features [+ "PDPA report incoming" for Pro]) so the buyer doesn't get a
        # bare "Activated" email on top of it. pdpa_monitor keeps this email — it
        # is the immediate "scan running" welcome, paired with the Monitor report
        # that follows when the scan completes.
        # Gated on `first_activation` so a dual webhook delivery / replay can't
        # send the activation email twice.
        if first_activation and customer_email and new_plan not in (
            "standard_suite", "pro_suite",
            "buyer_starter", "buyer_pro", "buyer_enterprise",
            "vendor_active", "vendor_pro",
        ):
            plan_labels = {
                "vendor_active": "Vendor Active",
                "pdpa_monitor": "PDPA Monitor",
                "enterprise": "Enterprise",
                "enterprise_pro": "Enterprise Pro",
                "standard_suite": "Standard Suite",
                "pro_suite": "Pro Suite",
                "evaluate_suppliers": "Evaluate Suppliers",
                "verify_supplier_evidence": "Verify Supplier Evidence",
                "compliance_evidence": "Compliance Evidence",
                "tender_intelligence": "Tender Intelligence",
                "vendor_pro": "Vendor Pro",
                "buyer_starter": "Buyer Essentials",
                "buyer_pro": "Buyer Professional",
                "buyer_enterprise": "Buyer Enterprise",
                "compliance_notarization_10": "Small Batch (10 notarizations/mo)",
                "compliance_notarization_50": "Enterprise Batch (50 notarizations/mo)",
            }
            label = plan_labels.get(new_plan, new_plan)

            # If PDPA Monitor, include either existing report link or "scan running" notice
            pdf_section = ""
            if new_plan == "pdpa_monitor":
                try:
                    from app.core.models import Report

                    pdpa_report = (
                        db.query(Report)
                        .filter(
                            Report.owner_id == user.id,
                            Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                            Report.status == "completed",
                        )
                        .order_by(Report.completed_at.desc())
                        .first()
                    )
                    if pdpa_report:
                        # Use stable download endpoint — not the presigned S3 URL which expires
                        download_url = f"https://api.booppa.io/api/v1/reports/{pdpa_report.id}/download"
                        pdf_section = f"""
                        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:16px;margin:16px 0;">
                          <p style="margin:0 0 8px;font-weight:bold;color:#0369a1;">Your latest PDPA report is ready</p>
                          <a href="{download_url}"
                             style="background:#0ea5e9;color:#fff;padding:10px 20px;text-decoration:none;
                                    border-radius:6px;font-weight:bold;display:inline-block;">
                            Download PDF Report &darr;
                          </a>
                        </div>
                        """
                    else:
                        # No existing report — first scan has been queued
                        pdf_section = """
                        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:16px;margin:16px 0;">
                          <p style="margin:0 0 8px;font-weight:bold;color:#0369a1;">Your first PDPA scan is running</p>
                          <p style="margin:0;color:#475569;font-size:14px;">
                            We're scanning your website now. You'll receive your PDF report by email
                            once it's ready (usually within a few minutes). You can also check your
                            <a href="https://www.booppa.io/vendor/dashboard" style="color:#0ea5e9;font-weight:bold;">dashboard</a>
                            for updates.
                          </p>
                        </div>
                        """
                except Exception as pdf_err:
                    logger.warning(
                        f"[Subscription] Could not fetch PDPA report for email: {pdf_err}"
                    )

            try:
                email_svc = EmailService()
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:20px;">{label} — Activated</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>Your <strong>{label}</strong> subscription is now active.</p>
                    {pdf_section}
                    <p>
                      <a href="https://www.booppa.io/vendor/dashboard"
                         style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                                border-radius:8px;font-weight:bold;display:inline-block;">
                        Go to Dashboard →
                      </a>
                    </p>
                    <p style="color:#64748b;font-size:12px;margin-top:24px;">booppa.io</p>
                  </div>
                </body></html>
                """
                await email_svc.send_html_email(
                    to_email=customer_email,
                    subject=f"Your {label} subscription is active — BOOPPA",
                    body_html=body_html,
                )
            except Exception as e:
                logger.error(f"[Subscription] Email failed for {customer_email}: {e}")

        # ── Trigger features based on plan ────────
        # Gated on `first_activation`: these branches queue scans, fulfil the
        # compliance-evidence cycle, initialise TRM controls, and send the suite
        # / buyer onboarding emails — all once-only side effects that must not
        # re-fire on a duplicate webhook delivery.
        if not first_activation:
            pass
        # NOTE: pdpa_monitor's first-cycle scan + Monitor report is fired by the
        # "Instant first-cycle delivery" block above (run_pdpa_monitor_cycle_for_user).
        # It used to ALSO fire here — a DUPLICATE scan that produced a second
        # Monitor report email per activation. Removed so each activation runs one
        # scan / one report.
        elif new_plan == "compliance_evidence":
            website = (getattr(user, "website", "") or "").strip()
            if not website:
                # Defer first-cycle fulfillment until the user adds a website —
                # PDPA + RFP regen has no target without `vendor_url`. Beat task
                # will pick them up next cycle once profile is updated.
                logger.warning(
                    f"[Subscription] CE activation deferred for {customer_email} — no website on profile"
                )
                try:
                    import asyncio as _asyncio

                    body_html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f8fafc;padding:24px;">
                    <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:8px;padding:28px;border:1px solid #e2e8f0;">
                      <h2 style="margin:0 0 12px;color:#0f172a;">Welcome to Compliance Evidence — one more step</h2>
                      <p style="color:#334155;line-height:1.55;">
                        Your subscription is active, but we need your website on file to generate
                        your PDPA Snapshot, RFP Complete Kit, and monthly Cover Sheet.
                      </p>
                      <a href="https://www.booppa.io/vendor/profile"
                         style="background:#0f172a;color:#fff;padding:10px 20px;text-decoration:none;
                                border-radius:6px;font-weight:bold;display:inline-block;margin-top:12px;">
                        Add your website
                      </a>
                      <p style="margin-top:24px;font-size:11px;color:#94a3b8;">
                        Once saved, your first cycle will run automatically.
                      </p>
                    </div></body></html>"""
                    _asyncio.run(
                        EmailService().send_html_email(
                            to_email=customer_email,
                            subject="Compliance Evidence: add your website to start your first cycle",
                            body_html=body_html,
                        )
                    )
                except Exception as email_exc:
                    logger.warning(
                        f"[Subscription] Could not send CE website-needed email: {email_exc}"
                    )
            # NOTE: when a website IS on file, the compliance_evidence first-cycle
            # bundle is fulfilled by the "Instant first-cycle delivery" block above
            # (run_compliance_evidence_cycle_for_user, which also resets the cycle
            # state). It used to ALSO fulfill here — a DUPLICATE bundle run (double
            # PDPA + RFP + cover sheet + emails). Removed. This branch now only
            # handles the no-website nudge above.
        elif new_plan in ["standard_suite", "pro_suite"]:
            try:
                from app.trm_workflow_service import initialise_trm_controls
                from app.api.vendor_features import _get_or_create_org
                from app.core.models import TrmControl

                org = _get_or_create_org(db, user)
                # Idempotent — skip if controls already exist (e.g. renewal webhook)
                existing = (
                    db.query(TrmControl)
                    .filter(TrmControl.organisation_id == org.id)
                    .count()
                )
                if existing == 0:
                    initialise_trm_controls(str(org.id), db)
                    logger.info(
                        f"[Subscription] Initialised MAS TRM controls for {customer_email} ({new_plan})"
                    )
                else:
                    logger.info(
                        f"[Subscription] TRM controls already present for {customer_email} ({existing} rows)"
                    )
            except Exception as e:
                logger.warning(
                    f"[Subscription] Failed to initialise MAS TRM controls: {e}"
                )

            # ── Onboarding email — itemise everything the suite unlocks with a
            # direct CTA per feature. Replaces the generic activation email
            # (gated off above). Sent synchronously so it also fires on the admin
            # simulate-purchase path, and doesn't depend on the Celery queue.
            if customer_email:
                try:
                    from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS

                    is_pro = new_plan == "pro_suite"
                    suite_label = "Pro Suite" if is_pro else "Standard Suite"
                    notar = ENTERPRISE_NOTARIZATION_LIMITS.get(new_plan, 50)

                    def _feature(title: str, desc: str, cta: str, url: str) -> str:
                        return f"""
                        <tr><td style="padding:14px 0;border-bottom:1px solid #1e293b;">
                          <p style="margin:0 0 4px;color:#fff;font-weight:bold;font-size:15px;">{title}</p>
                          <p style="margin:0 0 10px;color:#94a3b8;font-size:13px;line-height:1.5;">{desc}</p>
                          <a href="{url}" style="color:#10b981;font-weight:bold;text-decoration:none;font-size:13px;">{cta} &rarr;</a>
                        </td></tr>"""
                    features = [
                        _feature(
                            "MAS TRM — all 13 domains + baseline PDF",
                            "We've initialised all 13 MAS Technology Risk Management control domains for your "
                            "organisation and are emailing you a baseline assessment PDF shortly. Review and "
                            "work each domain in your TRM workspace.",
                            "Open TRM workspace", "https://www.booppa.io/vendor/trm",
                        ),
                        _feature(
                            "AI gap analysis (DeepSeek)",
                            "Run an AI-assisted gap analysis on any TRM domain — describe your current controls and "
                            "get a gap narrative, risk rating, and compliance status.",
                            "Run a gap analysis", "https://www.booppa.io/vendor/trm",
                        ),
                        _feature(
                            f"{notar} notarizations / month",
                            f"Your plan includes {notar} blockchain document notarizations every month. Upload any "
                            "compliance document to anchor a tamper-proof SHA-256 proof.",
                            "Notarize a document", "https://www.booppa.io/notarization",
                        ),
                        _feature(
                            "RESTful API + webhooks",
                            "Programmatic access to your compliance data. Create an API key and configure webhooks "
                            "to push events into your own systems.",
                            "Create an API key", "https://www.booppa.io/vendor/api-keys",
                        ),
                    ]
                    if is_pro:
                        features += [
                            _feature(
                                "SSO — SAML 2.0 + OIDC",
                                "Connect your identity provider so your team signs in with corporate credentials.",
                                "Configure SSO", "https://www.booppa.io/vendor/sso",
                            ),
                            _feature(
                                "White-label reports",
                                "Your reports and evidence packs now carry your own branding instead of Booppa's.",
                                "Manage branding", "https://www.booppa.io/settings",
                            ),
                            _feature(
                                "Multi-subsidiary management",
                                "Manage compliance across multiple legal entities from one account, each with its "
                                "own evidence and controls.",
                                "Manage subsidiaries", "https://www.booppa.io/vendor/subsidiaries",
                            ),
                        ]

                    onboarding_html = f"""
                    <html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
                    <div style="max-width:600px;margin:0 auto;">
                      <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
                        <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
                        <h1 style="margin:0;color:#10b981;font-size:22px;">{suite_label} — you're all set</h1>
                      </div>
                      <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
                        <p style="color:#cbd5e1;line-height:1.6;margin:0 0 18px;">
                          Your <strong>{suite_label}</strong> subscription is now active. Here's everything it unlocks and where to start:
                        </p>
                        <table style="width:100%;border-collapse:collapse;">{''.join(features)}</table>
                        <div style="text-align:center;margin:26px 0 6px;">
                          <a href="https://www.booppa.io/vendor/dashboard" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
                        </div>
                        <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
                      </div>
                    </div></body></html>"""

                    sent_ob = await EmailService().send_html_email(
                        to_email=customer_email,
                        subject=f"Welcome to {suite_label} — here's everything included",
                        body_html=onboarding_html,
                    )
                    if not sent_ob:
                        logger.error(
                            f"[Subscription] Suite onboarding email rejected by provider "
                            f"for {customer_email} ({new_plan})"
                        )
                    else:
                        logger.info(
                            f"[Subscription] Sent {suite_label} onboarding email to {customer_email}"
                        )
                except Exception as ob_err:
                    logger.warning(
                        f"[Subscription] Suite onboarding email failed for {customer_email}: {ob_err}"
                    )

        elif new_plan in ("buyer_starter", "buyer_pro", "buyer_enterprise") and customer_email:
            # Buyer-tier onboarding email — itemise the due-diligence features
            # the tier unlocks with a direct CTA each. Only ships features that
            # are actually wired (see HANDOFF audit); marketed-but-unbuilt items
            # (custom risk weights, native Slack/Teams, custom frameworks) are
            # intentionally omitted so the welcome email has no dead links.
            try:
                from app.billing.enforcement import scan_limit_for, max_seats_for
                from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS

                labels = {
                    "buyer_starter": "Buyer Essentials",
                    "buyer_pro": "Buyer Professional",
                    "buyer_enterprise": "Buyer Enterprise",
                }
                buyer_label = labels[new_plan]
                quick = scan_limit_for(new_plan, "QUICK") or 0
                deep = scan_limit_for(new_plan, "DEEP") or 0
                evidence = scan_limit_for(new_plan, "EVIDENCE") or 0
                notar = ENTERPRISE_NOTARIZATION_LIMITS.get(new_plan, 1)
                seats = max_seats_for(new_plan)
                seats_txt = "Unlimited seats with RBAC" if seats is None else (
                    f"{seats} seats with role-based access" if seats > 1 else "1 user seat"
                )
                dash = "https://www.booppa.io/procurement/dashboard"

                def _bf(title: str, desc: str, cta: str, url: str) -> str:
                    return f"""
                    <tr><td style="padding:14px 0;border-bottom:1px solid #1e293b;">
                      <p style="margin:0 0 4px;color:#fff;font-weight:bold;font-size:15px;">{title}</p>
                      <p style="margin:0 0 10px;color:#94a3b8;font-size:13px;line-height:1.5;">{desc}</p>
                      <a href="{url}" style="color:#10b981;font-weight:bold;text-decoration:none;font-size:13px;">{cta} &rarr;</a>
                    </td></tr>"""

                feats = []
                scan_line = f"Quick Scan on {quick} vendors/month (ACRA + MAS watchlist + PDPA flag)"
                if deep:
                    scan_line = (f"{quick} Quick Scans + {deep} Deep Scans/month "
                                 "(11-dimension PDPA + certifications + financial risk)")
                feats.append(_bf("Vendor scans", scan_line, "Start scanning", dash))
                if evidence:
                    feats.append(_bf(
                        f"Evidence Scan — {evidence} vendors/month",
                        "Level-3 blockchain evidence retrieval + complete vendor dossier.",
                        "Run an Evidence Scan", dash,
                    ))
                feats.append(_bf(
                    "Compliance dashboard",
                    "Traffic-light status across every vendor you scan, with automatic alerts when one enters critical status.",
                    "Open dashboard", dash,
                ))
                feats.append(_bf(
                    "Vendor directory",
                    "Browse the vendor network with advanced filters (sector, size, certifications).",
                    "Browse vendors", dash,
                ))
                if deep:
                    feats.append(_bf(
                        "Comparison engine + drift tracking",
                        "Compare vendors side-by-side across Deep Scan parameters, with automatic change alerts as their compliance drifts.",
                        "Compare vendors", "https://www.booppa.io/compare",
                    ))
                export_desc = ("CSV export of scan results for tender spreadsheets."
                               if not deep else
                               "CSV export plus exportable Deep Scan PDF reports for shortlists and tender minutes.")
                feats.append(_bf("Exports", export_desc, "Export results", dash))
                if new_plan == "buyer_enterprise":
                    feats.append(_bf(
                        "Multi-subsidiary management",
                        "Manage due diligence across multiple BUs / legal entities from one account.",
                        "Manage subsidiaries", "https://www.booppa.io/vendor/subsidiaries",
                    ))
                    feats.append(_bf(
                        "White-label reports",
                        "Board- and regulator-ready reports carrying your own branding.",
                        "Manage branding", "https://www.booppa.io/settings",
                    ))
                    feats.append(_bf(
                        "RESTful API + webhooks",
                        "Programmatic access for ERP integration. Create an API key and configure webhooks.",
                        "Create an API key", "https://www.booppa.io/vendor/api-keys",
                    ))
                elif new_plan == "buyer_pro":
                    feats.append(_bf(
                        "Webhook integrations",
                        "Push scan + drift events into your own systems (email, or any incoming-webhook URL such as Slack or Teams).",
                        "Configure webhooks", "https://www.booppa.io/vendor/api-keys",
                    ))
                feats.append(_bf(
                    f"{notar} notarization{'s' if notar != 1 else ''} / month",
                    "Anchor any compliance document on the blockchain with a tamper-proof SHA-256 proof.",
                    "Notarize a document", "https://www.booppa.io/notarization",
                ))

                onboarding_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
                <div style="max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
                    <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
                    <h1 style="margin:0;color:#10b981;font-size:22px;">{buyer_label} — you're all set</h1>
                  </div>
                  <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
                    <p style="color:#cbd5e1;line-height:1.6;margin:0 0 8px;">
                      Your <strong>{buyer_label}</strong> subscription is now active — {seats_txt}. Here's everything it unlocks and where to start:
                    </p>
                    <table style="width:100%;border-collapse:collapse;">{''.join(feats)}</table>
                    <div style="text-align:center;margin:26px 0 6px;">
                      <a href="{dash}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
                    </div>
                    <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
                  </div>
                </div></body></html>"""

                sent_ob = await EmailService().send_html_email(
                    to_email=customer_email,
                    subject=f"Welcome to {buyer_label} — here's everything included",
                    body_html=onboarding_html,
                )
                if not sent_ob:
                    logger.error(
                        f"[Subscription] Buyer onboarding email rejected by provider "
                        f"for {customer_email} ({new_plan})"
                    )
                else:
                    logger.info(
                        f"[Subscription] Sent {buyer_label} onboarding email to {customer_email}"
                    )
            except Exception as ob_err:
                logger.warning(
                    f"[Subscription] Buyer onboarding email failed for {customer_email}: {ob_err}"
                )

        # Record activation in ActivityLog so Engagement + Recency move.
        _log_purchase_activity(
            db,
            user.id,
            activity_type="SUBSCRIPTION_ACTIVATED",
            description=f"Subscription activated: {new_plan}",
            extra={"product_type": product_type, "plan": new_plan},
        )

        # Elevate verification level for the duration of this subscription so
        # the trust-score compliance multiplier reflects the paid tier.
        try:
            _apply_subscription_score_lever(db, user.id, new_plan)
        except Exception as lever_err:
            logger.warning(
                f"[Subscription] Score lever apply failed for {customer_email}: {lever_err}"
            )

    except Exception as e:
        logger.error(f"[Subscription] Activation error for {product_type}: {e}")
        db.rollback()
    finally:
        db.close()


