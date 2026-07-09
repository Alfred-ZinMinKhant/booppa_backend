from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service
from app.services.email_service import EmailService
from app.billing.enforcement import enforce_tier
from app.core.models_v10 import Referral
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


def _log_purchase_activity(
    db, vendor_id, activity_type: str, description: str, extra: dict | None = None
) -> None:
    """Record a row in ActivityLog so Engagement + Recency score components
    reflect paid actions (purchases, renewals, fulfillments)."""
    if not vendor_id:
        return
    try:
        from app.core.models_v6 import ActivityLog

        db.add(
            ActivityLog(
                user_id=vendor_id,
                type=activity_type,
                description=description[:500],
                metadata_json=extra or {},
            )
        )
        db.commit()
    except Exception as e:
        logger.warning(
            f"[Activity] log insert failed for vendor={vendor_id} type={activity_type}: {e}"
        )


def _apply_subscription_score_lever(db, vendor_id, plan: str) -> None:
    """On subscription activation, elevate VerifyRecord.verification_level to
    the plan's tier (only if higher than current), then recalculate the score."""
    target = _PLAN_TO_VERIFICATION_LEVEL.get(plan)
    if not target or not vendor_id:
        return
    try:
        from app.core.models_v6 import VerifyRecord as _VR, VerificationLevel as _VL

        vr = db.query(_VR).filter(_VR.vendor_id == vendor_id).first()
        if not vr:
            # Vendor hasn't completed vendor_proof yet — the lever will apply
            # when that fulfillment creates the VerifyRecord.
            logger.info(
                f"[Subscription] No VerifyRecord for vendor={vendor_id}; "
                f"score lever deferred (plan={plan})"
            )
            return
        current = vr.verification_level.value if vr.verification_level else "BASIC"
        if _LEVEL_RANK.get(target, 0) > _LEVEL_RANK.get(current, 0):
            vr.verification_level = _VL[target]
            db.commit()
            logger.info(
                f"[Subscription] Elevated VerifyRecord.verification_level "
                f"{current} → {target} for vendor={vendor_id} (plan={plan})"
            )
    except Exception as e:
        logger.warning(
            f"[Subscription] verification_level elevation failed for vendor={vendor_id}: {e}"
        )
    try:
        from app.services.scoring import VendorScoreEngine

        VendorScoreEngine.update_vendor_score(db, vendor_id)
    except Exception as e:
        logger.warning(
            f"[Subscription] Score recalc failed for vendor={vendor_id}: {e}"
        )


def _revert_subscription_score_lever(db, vendor_id, remaining_plan: str | None) -> None:
    """On subscription cancellation, recompute verification_level as
    max(remaining-plan-tier, notarization-depth-tier) so vendors who earned
    their tier through proofs don't lose it when their sub lapses."""
    if not vendor_id:
        return
    try:
        from app.core.models_v6 import VerifyRecord as _VR, VerificationLevel as _VL
        from app.services.vendor_status import compute_verification_depth

        vr = db.query(_VR).filter(_VR.vendor_id == vendor_id).first()
        if not vr:
            return
        depth_to_level = {
            "STANDARD": "STANDARD",
            "DEEP": "PREMIUM",
            "CERTIFIED": "GOVERNMENT",
            "ENTERPRISE": "GOVERNMENT",
        }
        depth_level = depth_to_level.get(
            compute_verification_depth(db, str(vendor_id)), "BASIC"
        )
        plan_level = _PLAN_TO_VERIFICATION_LEVEL.get(remaining_plan or "", "BASIC")
        winner = max(
            (depth_level, plan_level),
            key=lambda lvl: _LEVEL_RANK.get(lvl, 0),
        )
        current = vr.verification_level.value if vr.verification_level else "BASIC"
        if current != winner:
            vr.verification_level = _VL[winner]
            db.commit()
            logger.info(
                f"[Subscription] Reverted verification_level {current} → {winner} "
                f"for vendor={vendor_id} (depth={depth_level}, remaining_plan={remaining_plan})"
            )
    except Exception as e:
        logger.warning(
            f"[Subscription] verification_level revert failed for vendor={vendor_id}: {e}"
        )
    try:
        from app.services.scoring import VendorScoreEngine

        VendorScoreEngine.update_vendor_score(db, vendor_id)
    except Exception as e:
        logger.warning(
            f"[Subscription] Score recalc failed for vendor={vendor_id}: {e}"
        )


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
            from app.core.models_enterprise import Organisation as _Org

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
                from app.core.models_enterprise import TrmControl

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
                    from app.core.models_v8 import ENTERPRISE_NOTARIZATION_LIMITS

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
                from app.core.models_v8 import ENTERPRISE_NOTARIZATION_LIMITS

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


async def _alert_payment_fulfillment_issue(
    *,
    reason: str,
    product_type: str | None,
    customer_email: str | None,
    session_id: str | None = None,
    event_id: str | None = None,
    extra: dict | None = None,
    notify_customer: bool = True,
) -> None:
    """Loud failure path for any post-payment branch that can't complete fulfillment.

    The user already paid — silently logging a warning is not enough. This:
      1. Logs at ERROR with full context.
      2. Emails settings.SUPPORT_EMAIL so the team can resolve manually.
      3. Optionally emails the customer ("we received your payment, on it") so
         they're not left wondering when the email never arrives.

    All sends are best-effort: failure of an alert email never propagates.
    """
    extra_str = ""
    if extra:
        try:
            extra_str = json.dumps(extra, default=str, sort_keys=True)[:1000]
        except Exception:
            extra_str = str(extra)[:1000]
    logger.error(
        f"[Fulfillment-ALERT] reason={reason} product={product_type} "
        f"email={customer_email} session={session_id} event={event_id} extra={extra_str}"
    )

    try:
        body_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;">
          <h2 style="color:#b91c1c;">Fulfillment alert — manual review needed</h2>
          <p>A Stripe payment landed in a branch that could not complete automatic fulfillment.</p>
          <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <tr><td style="padding:6px 8px;color:#64748b;">Reason</td><td style="padding:6px 8px;"><strong>{reason}</strong></td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Product</td><td style="padding:6px 8px;">{product_type or '(unknown)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Customer email</td><td style="padding:6px 8px;">{customer_email or '(missing)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Stripe session</td><td style="padding:6px 8px;font-family:monospace;">{session_id or '(missing)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Webhook event</td><td style="padding:6px 8px;font-family:monospace;">{event_id or '(unknown)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Extra</td><td style="padding:6px 8px;font-family:monospace;">{extra_str or '-'}</td></tr>
          </table>
        </div>
        """
        await EmailService().send_html_email(
            to_email=settings.SUPPORT_EMAIL,
            subject=f"[FULFILLMENT] {reason} ({product_type or '?'})",
            body_html=body_html,
        )
    except Exception as alert_err:
        logger.error(f"[Fulfillment-ALERT] ops email failed: {alert_err}")

    if notify_customer and customer_email:
        try:
            await EmailService().send_html_email(
                to_email=customer_email,
                subject="We received your payment — one small delay",
                body_html=f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
                  <h2 style="color:#0f172a;">Thank you for your purchase</h2>
                  <p style="color:#334155;">
                    Your payment for <strong>{(product_type or '').replace('_', ' ').title() or 'your order'}</strong>
                    has been received. We hit a small snag finalising your account and our team
                    has been alerted — we will follow up within a few hours and make sure
                    everything is sorted.
                  </p>
                  <p style="color:#334155;">
                    If you have any questions in the meantime, just reply to this email.
                  </p>
                  <p style="color:#64748b;font-size:13px;margin-top:24px;">
                    Order reference: <span style="font-family:monospace;">{session_id or 'n/a'}</span>
                  </p>
                </div>
                """,
            )
        except Exception as cust_err:
            logger.warning(f"[Fulfillment-ALERT] customer email failed: {cust_err}")


def _create_stub_report(
    db,
    *,
    framework: str,
    owner_id,
    company_name: str | None,
    website: str | None,
    customer_email: str | None,
    source: str,
    session_id: str | None = None,
    test_simulation: bool = False,
    uen: str | None = None,
) -> str:
    """Create a synthetic Report row for fulfillment paths that don't have a
    pre-existing report (standalone /pricing purchases, bundle components).

    Caller is responsible for committing the surrounding session.
    """
    from app.core.models import Report
    import uuid as _uuid

    assessment: dict = {
        "payment_confirmed": True,
        "on_page_only": False,
        "tier": "pro",
        "contact_email": customer_email,
        "bundle_source": source,
    }
    if uen:
        # ACRA-gated UEN from checkout → certificate shows the verified number.
        assessment["uen"] = uen
    if session_id:
        # Stored so /api/reports/by-session can resolve the stub even when the
        # Stripe metadata backfill failed.
        assessment["stripe_session_id"] = session_id
    if test_simulation:
        assessment["test_simulation"] = True
    stub = Report(
        owner_id=owner_id or _uuid.uuid4(),
        framework=framework,
        company_name=company_name,
        company_website=website,
        status="pending",
        assessment_data=assessment,
    )
    db.add(stub)
    db.flush()
    return str(stub.id)


async def _defer_rfp_to_intake(
    *,
    rfp_product_type: str,
    bundle_source: str,
    customer_email: str | None,
    vendor_url: str | None,
    company_name: str | None,
    session_id: str | None,
) -> str | None:
    """Create a PendingRfpIntake row and email the buyer a link to /rfp-intake/{id}.

    Used when an RFP-bearing purchase arrives without a `rfp_description` — bundles
    bought from /pricing, plus standalone rfp_express/rfp_complete one-click buys.
    Caller need not commit; this helper owns its own session. Returns intake_id
    or None if no user could be resolved.
    """
    if not customer_email:
        await _alert_payment_fulfillment_issue(
            reason="RFP purchase paid but webhook had no customer_email",
            product_type=rfp_product_type,
            customer_email=None,
            session_id=session_id,
            extra={"bundle_source": bundle_source},
            notify_customer=False,
        )
        return None
    from app.core.models_v12 import PendingRfpIntake

    db = SessionLocal()
    intake_id: str | None = None
    try:
        user = db.query(User).filter(User.email == customer_email).first()
        if not user:
            await _alert_payment_fulfillment_issue(
                reason="RFP purchase paid but no user row matched customer_email",
                product_type=rfp_product_type,
                customer_email=customer_email,
                session_id=session_id,
                extra={"bundle_source": bundle_source},
            )
            return None
        resolved_url = vendor_url or (getattr(user, "website", "") or "") or None
        resolved_company = company_name or (getattr(user, "company", "") or "") or None
        pending = PendingRfpIntake(
            user_id=user.id,
            session_id=session_id,
            rfp_product_type=rfp_product_type,
            bundle_source=bundle_source,
            vendor_url=resolved_url,
            company_name=resolved_company,
            status="pending",
        )
        db.add(pending)
        db.flush()
        intake_id = str(pending.id)
        db.commit()
        logger.info(
            f"[RFP-defer:{rfp_product_type}] Created PendingRfpIntake {intake_id} for {customer_email}"
        )
    except Exception as e:
        logger.error(f"[RFP-defer:{rfp_product_type}] DB error: {e}")
        db.rollback()
        return None
    finally:
        db.close()

    try:
        kit_label = (
            "RFP Complete Kit"
            if rfp_product_type == "rfp_complete"
            else "RFP Express Kit"
        )
        intake_url = f"https://www.booppa.io/rfp-intake/{intake_id}"
        sent = await EmailService().send_html_email(
            to_email=customer_email,
            subject=f"One more step: complete your {kit_label} brief",
            body_html=f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
              <h2 style="color:#0f172a;">Tell us about your RFP</h2>
              <p style="color:#334155;">
                Thanks for your purchase. Share a few details about the procurement and
                we'll generate your <strong>{kit_label}</strong>.
              </p>
              <div style="text-align:center;margin:24px 0;">
                <a href="{intake_url}"
                   style="display:inline-block;background:#0ea5e9;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;">
                  Complete your RFP brief
                </a>
              </div>
              <p style="color:#64748b;font-size:13px;">
                Takes about 2 minutes. Your kit is generated as soon as you submit.
              </p>
            </div>
            """,
        )
        # send_html_email returns False on Resend/SES rejection without
        # raising — the buyer would never see the brief link and the page
        # would sit on "Confirming purchase…". Surface it via the standard
        # fulfillment alert so it isn't silently lost.
        if not sent:
            logger.error(
                f"[RFP-defer:{rfp_product_type}] Intake email rejected by provider "
                f"for {customer_email} (intake_id={intake_id})"
            )
            await _alert_payment_fulfillment_issue(
                reason="Intake email rejected by email provider",
                product_type=rfp_product_type,
                customer_email=customer_email,
                session_id=session_id,
                extra={"intake_id": intake_id, "bundle_source": bundle_source},
                notify_customer=False,
            )
        else:
            logger.info(
                f"[RFP-defer:{rfp_product_type}] Sent intake email to {customer_email} "
                f"(intake_id={intake_id})"
            )
    except Exception as email_err:
        logger.warning(
            f"[RFP-defer:{rfp_product_type}] Intake email failed: {email_err}"
        )

    return intake_id


async def _fulfill_standalone_no_report(
    product_type: str,
    customer_email: str | None,
    metadata: dict,
    session_id: str | None = None,
) -> bool:
    """Fulfillment path for /pricing-direct purchases that arrived without a
    pre-existing Report (pdpa_quick_scan, vendor_proof) or that grant credits
    (compliance_notarization_*).

    Returns True if the product was handled here, False if the caller should
    fall through to other branches.
    """
    if product_type not in (
        PDPA_PRODUCT_TYPES | VENDOR_PROOF_PRODUCT_TYPES | NOTARIZATION_PRODUCT_TYPES
        | CSP_ONETIME_PRODUCT_TYPES
    ):
        return False

    # CSP one-time pack purchase: grant lifetime pack access on the org.
    if product_type in CSP_ONETIME_PRODUCT_TYPES:
        db = SessionLocal()
        try:
            user = (
                db.query(User).filter(User.email == customer_email).first()
                if customer_email else None
            )
            if not user:
                await _alert_payment_fulfillment_issue(
                    reason="CSP one-time purchase paid but no user matched customer_email",
                    product_type=product_type,
                    customer_email=customer_email,
                    session_id=session_id,
                )
                return True
            from app.services.csp_access import activate_csp_access

            activate_csp_access(db, user=user, plan="csp", billing_type="one_time")
            logger.info(f"[CSP] One-time pack access granted to {customer_email}")
            try:
                sent = await EmailService().send_html_email(
                    user.email,
                    "Your CSP Compliance Pack is active",
                    _csp_activation_email_html("csp"),
                )
                if not sent:
                    await _alert_payment_fulfillment_issue(
                        reason="CSP one-time activated but activation email rejected by provider",
                        product_type=product_type,
                        customer_email=customer_email,
                        session_id=session_id,
                    )
            except Exception as e:
                logger.warning(f"[CSP] one-time activation email failed: {e}")
            return True
        finally:
            db.close()

    company_name = (metadata.get("company_name") or "").strip()
    website = (metadata.get("vendor_url") or metadata.get("website_url") or "").strip()

    db = SessionLocal()
    try:
        owner_id = None
        user = None
        if customer_email:
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                owner_id = user.id
                if not company_name:
                    company_name = (getattr(user, "company", "") or "").strip()
                if not website:
                    website = (getattr(user, "website", "") or "").strip()

        # Notarization credits: grant balance, send redemption email.
        if product_type in NOTARIZATION_PRODUCT_TYPES:
            count = NOTARIZATION_CREDIT_AMOUNTS.get(product_type, 0)
            if not customer_email or not user:
                await _alert_payment_fulfillment_issue(
                    reason=f"notarization purchase paid but cannot grant {count} credits — no user found",
                    product_type=product_type,
                    customer_email=customer_email,
                    extra={"credits_intended": count},
                )
                return True
            locked = db.query(User).filter(User.id == user.id).with_for_update().first()
            current = getattr(locked, "notarization_credits", 0) or 0
            locked.notarization_credits = current + count
            db.commit()
            logger.info(
                f"[Notarize:{product_type}] Granted {count} credits to {customer_email} "
                f"(balance: {current} → {locked.notarization_credits})"
            )
            try:
                sent = await EmailService().send_html_email(
                    to_email=customer_email,
                    subject=f"Your {count} notarization{'s' if count != 1 else ''} ready to redeem",
                    body_html=f"""
                    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
                      <h2 style="color:#0f172a;">Notarization credits issued</h2>
                      <p style="color:#334155;">
                        Thanks for your purchase. You now have
                        <strong>{count} notarization credit{'s' if count != 1 else ''}</strong>
                        on your account. Each lets you anchor any compliance document (PDF, DOCX, image, etc.)
                        on the blockchain with SHA-256 proof.
                      </p>
                      <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:16px;margin:20px 0;">
                        <p style="margin:0 0 12px;font-weight:bold;color:#0369a1;">How to redeem</p>
                        <p style="margin:0;color:#334155;font-size:14px;">
                          Visit <a href="https://www.booppa.io/notarize" style="color:#0ea5e9;font-weight:bold;">booppa.io/notarize</a>,
                          upload your document, and enter this email ({customer_email}).
                          Your credit will be applied automatically — no payment required.
                        </p>
                      </div>
                      <p style="color:#64748b;font-size:13px;">
                        Credits don't expire. You can use them one at a time or all at once.
                      </p>
                    </div>
                    """,
                )
                if sent:
                    logger.info(
                        f"[Notarize:{product_type}] Sent credits-granted email to {customer_email}"
                    )
                else:
                    # Credits are already on the account, but the buyer was never
                    # told how to redeem them — surface to ops (and re-notify buyer).
                    await _alert_payment_fulfillment_issue(
                        reason="notarization credits-granted email rejected by provider",
                        product_type=product_type,
                        customer_email=customer_email,
                        session_id=session_id,
                        extra={"credits": count},
                    )
            except Exception as email_err:
                logger.warning(
                    f"[Notarize:{product_type}] Credits email failed: {email_err}"
                )
            return True

        # PDPA / Vendor Proof: create stub Report and queue the fulfillment task.
        framework = (
            "pdpa_quick_scan" if product_type in PDPA_PRODUCT_TYPES else "vendor_proof"
        )
        if not website and framework == "pdpa_quick_scan":
            await _alert_payment_fulfillment_issue(
                reason="PDPA Snapshot paid but no website found on metadata or profile",
                product_type=product_type,
                customer_email=customer_email,
            )
            return True
        stub_id = _create_stub_report(
            db,
            framework=framework,
            owner_id=owner_id,
            company_name=company_name or None,
            website=website or None,
            customer_email=customer_email,
            source=product_type,
            session_id=session_id,
            test_simulation=bool(metadata.get("test_simulation")),
            uen=(metadata.get("uen") or "").strip() or None,
        )
        db.commit()

        # Backfill Stripe session metadata so /api/reports/by-session can resolve
        # the stub report on the user's result page. The session was created without
        # a report_id (the stub didn't exist yet); without this backfill the result
        # page would 404-poll until timeout even though fulfillment succeeded.
        if session_id:
            try:
                stripe.api_key = settings.STRIPE_SECRET_KEY
                stripe.checkout.Session.modify(
                    session_id, metadata={**(metadata or {}), "report_id": stub_id}
                )
                logger.info(
                    f"[Standalone:{product_type}] Backfilled report_id={stub_id} onto session {session_id}"
                )
            except Exception as backfill_err:
                logger.warning(
                    f"[Standalone:{product_type}] Could not backfill session metadata: {backfill_err}"
                )

        if framework == "vendor_proof":
            from app.workers.tasks import fulfill_vendor_proof_task

            fulfill_vendor_proof_task.delay(stub_id, customer_email)
            logger.info(
                f"[Standalone:vendor_proof] Queued fulfillment for stub report {stub_id} "
                f"(email={customer_email})"
            )
        else:
            from app.workers.tasks import fulfill_pdpa_task

            fulfill_pdpa_task.delay(stub_id, customer_email)
            logger.info(
                f"[Standalone:{product_type}] Queued PDPA fulfillment for stub report {stub_id} "
                f"(email={customer_email}, website={website})"
            )
        return True
    except Exception as e:
        logger.exception(f"[Standalone:{product_type}] Fulfillment error: {e}")
        db.rollback()
        await _alert_payment_fulfillment_issue(
            reason=f"standalone fulfillment raised exception: {type(e).__name__}: {e}",
            product_type=product_type,
            customer_email=customer_email,
        )
        return True  # still consumed — don't let the caller double-process
    finally:
        db.close()


async def _fulfill_compliance_evidence_pack(
    db,
    owner_id,
    customer_email: str | None,
    company_name: str,
    website: str,
    session_id: str | None,
    metadata: dict,
    is_test: bool,
    send_email: bool = True,
):
    """Create the EvidencePack intake for a Compliance Evidence Pack purchase.

    Real purchases defer to the structured intake at /evidence-pack-intake/{id}.
    Admin test simulations auto-build an intake from the profile/test identity and
    queue generation immediately (so the test harness yields a pack end-to-end).

    Returns the created EvidencePack row (or None if no owner). When `send_email`
    is False the standalone intake email is suppressed so the caller can fold the
    intake CTA into the single consolidated bundle email instead.
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    from app.core.models import User
    from app.core.models_v13 import EvidencePack

    if not owner_id:
        await _alert_payment_fulfillment_issue(
            reason="compliance_evidence_pack paid but no owner resolved — cannot create EvidencePack",
            product_type="compliance_evidence_pack",
            customer_email=customer_email,
            session_id=session_id,
        )
        return None

    org = (company_name or "").strip() or "Your Organisation"
    pack_id = f"BCEP-{org.upper().replace(' ', '')[:8]}-{_dt.utcnow().strftime('%Y%m%d%H%M%S')}"
    row = EvidencePack(
        id=_uuid.uuid4(),
        pack_id=pack_id,
        user_id=owner_id,
        session_id=session_id,
        organisation=org,
        status="intake_pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if is_test:
        # Auto-build a usable intake from the buyer profile + test identity so the
        # admin test-checkout produces a full pack without a manual intake step.
        user = db.query(User).filter(User.id == owner_id).first()
        intake = {
            "org_name": org,
            "uen": (getattr(user, "uen", "") or "").strip() or metadata.get("uen") or "Not provided",
            "domain": (website or getattr(user, "website", "") or "").replace("https://", "").replace("http://", "").strip("/"),
            "sector": metadata.get("sector") or "Professional Services",
            "employee_count": metadata.get("employee_count") or "11-50",
            "dpo_name": metadata.get("dpo_name") or "To be designated",
            "dpo_email": metadata.get("dpo_email") or "",
            "approver_name": metadata.get("approver_name") or (getattr(user, "full_name", "") or "Authorised Representative"),
            "approver_role": metadata.get("approver_role") or "Director",
            "data_types": ["customer data", "employee data", "vendor data"],
            "customer_types": ["B2B clients"],
            "systems": ["AWS", "Google Workspace", "Stripe"],
            "cloud_provider": "AWS",
            "other_markets": "",
            "it_contact": "IT Manager",
        }
        row.intake = intake
        row.status = "queued"
        db.commit()
        from app.workers.tasks import fulfill_evidence_pack_task
        fulfill_evidence_pack_task.delay(str(row.id))
        logger.info("[Bundle:compliance_evidence_pack] test_simulation — auto-queued pack %s", pack_id)
        return row

    # Subscription renewal: reuse the buyer's most recent completed intake so they
    # don't re-fill the form every month — regenerate against last cycle's facts.
    if metadata.get("subscription_cycle"):
        prior = (
            db.query(EvidencePack)
            .filter(
                EvidencePack.user_id == owner_id,
                EvidencePack.id != row.id,
                EvidencePack.intake.isnot(None),
            )
            .order_by(EvidencePack.created_at.desc())
            .first()
        )
        if prior and isinstance(prior.intake, dict) and prior.intake.get("org_name"):
            row.intake = prior.intake
            row.status = "queued"
            db.commit()
            from app.workers.tasks import fulfill_evidence_pack_task
            fulfill_evidence_pack_task.delay(str(row.id))
            logger.info("[Bundle:compliance_evidence_pack] cycle — reused prior intake, queued %s", pack_id)
            return row

    # Real purchase (or first cycle with no prior intake) — email the buyer a link
    # to complete the structured intake. Suppressed when the caller will fold the
    # intake CTA into the consolidated bundle email (send_email=False).
    if send_email and customer_email:
        intake_url = f"https://www.booppa.io/evidence-pack-intake/{row.id}"
        body_html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:620px;margin:0 auto;">
          <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
            <h1 style="color:#10b981;margin:0;font-size:20px;">Start your PDPA Compliance Evidence Pack</h1>
          </div>
          <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
            <p>Thank you for your purchase. Your Evidence Pack builds seven PDPA governance
               documents — DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook,
               Training Register, and Security Review Log — tailored to your organisation.</p>
            <p>To generate documents that reflect your actual operations, we need a short
               structured intake (about 5 minutes): your org details, DPO, systems, data types,
               and where data is hosted.</p>
            <div style="text-align:center;margin:24px 0;">
              <a href="{intake_url}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Complete your intake →</a>
            </div>
            <p style="color:#64748b;font-size:12px;">Every document is an AI-generated DRAFT with no
               evidentiary value until your authorised representative reviews and signs it.</p>
          </div>
        </body></html>"""
        sent = await EmailService().send_html_email(
            to_email=customer_email,
            subject="Action needed: start your PDPA Compliance Evidence Pack",
            body_html=body_html,
        )
        if not sent:
            logger.error("[Bundle:compliance_evidence_pack] intake email rejected for %s", customer_email)
    logger.info("[Bundle:compliance_evidence_pack] Created intake-pending pack %s for %s", pack_id, customer_email)
    return row


async def _fulfill_bundle(
    product_type: str,
    report_id: str | None,
    customer_email: str | None,
    metadata: dict,
    session_id: str | None,
) -> None:
    """
    Bundle fulfillment: fan out to multiple fulfillment tasks based on BUNDLE_COMPONENTS.
    Each component is queued as its own Celery task with retry semantics.
    """
    components = BUNDLE_COMPONENTS.get(product_type)
    if not components:
        logger.error(f"[Bundle] Unknown bundle product_type={product_type}")
        return

    from app.workers.tasks import (
        fulfill_vendor_proof_task,
        fulfill_pdpa_task,
    )
    from app.core.models_v12 import PendingRfpIntake

    db = SessionLocal()
    try:
        from app.core.models import Report

        base_report = (
            db.query(Report).filter(Report.id == report_id).first()
            if report_id
            else None
        )
        owner_id = base_report.owner_id if base_report else None

        # Resolve owner from customer_email when no base report exists (all bundle purchases)
        if not owner_id and customer_email:
            from app.core.models import User

            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                owner_id = user.id
                logger.info(
                    f"[Bundle:{product_type}] Resolved owner_id={owner_id} from email={customer_email}"
                )
            else:
                await _alert_payment_fulfillment_issue(
                    reason="bundle paid but no user row matched customer_email — stubs would be orphaned",
                    product_type=product_type,
                    customer_email=customer_email,
                    session_id=session_id,
                )
                return  # don't create orphan stub Reports

        company_name = (
            base_report.company_name if base_report else None
        ) or metadata.get("company_name", "")
        website = (
            base_report.company_website if base_report else None
        ) or metadata.get("vendor_url", "")

        _is_test = bool(metadata.get("test_simulation"))

        # ── Compliance Evidence Pack → BCEP 7-document engine ────────────────
        # The CE SKU no longer produces the cover-sheet bundle (PDPA + RFP + cover
        # sheet). It now produces the BCEP governance pack (DPMP, ROPA, Data
        # Inventory, Vendor/DPA Register, Breach Runbook, Training Register,
        # Security Review Log), which closes PDPC Levels 2-6. Generation needs a
        # structured intake, so we defer exactly like the RFP flow: create an
        # EvidencePack row (status=intake_pending) and email the buyer a brief link.
        # CE creates the BCEP 7-document intake here, then FALLS THROUGH to the
        # generic fan-out below so the declared PDPA scan + RFP Complete kit +
        # cover-sheet credit are also delivered (BUNDLE_COMPONENTS). The standalone
        # intake email is suppressed (send_email=False) — its CTA is folded into the
        # single consolidated bundle email to avoid double-emailing the buyer.
        evidence_pack_row = None
        if product_type == "compliance_evidence_pack":
            evidence_pack_row = await _fulfill_compliance_evidence_pack(
                db=db,
                owner_id=owner_id,
                customer_email=customer_email,
                company_name=company_name,
                website=website,
                session_id=session_id,
                metadata=metadata,
                is_test=_is_test,
                send_email=False,
            )

        def _make_stub(framework: str) -> str:
            return _create_stub_report(
                db,
                framework=framework,
                owner_id=owner_id,
                company_name=company_name,
                website=website,
                customer_email=customer_email,
                source=product_type,
                session_id=session_id,
                test_simulation=_is_test,
            )

        # Collect all stub IDs before committing — single atomic commit for all stubs
        tasks_to_queue = []

        # 1. Vendor Proof
        if components.get("vendor_proof"):
            vp_id = (
                str(base_report.id)
                if base_report and base_report.framework in ("vendor_proof",)
                else _make_stub("vendor_proof")
            )
            tasks_to_queue.append(("vendor_proof", vp_id))

        # 2. PDPA Snapshot
        if components.get("pdpa"):
            pdpa_id = _make_stub("pdpa_quick_scan")
            tasks_to_queue.append(("pdpa", pdpa_id))

        # 2b. RFP Kit — defer to a post-checkout intake step. We don't have the
        # buyer's RFP description at this point (bundle checkouts don't collect it),
        # so create a PendingRfpIntake row that the user completes at /rfp-intake/{id}.
        # The intake endpoint queues fulfill_rfp_task once they submit the brief.
        rfp_product = components.get("rfp")
        pending_intake_id: str | None = None
        if rfp_product and owner_id and _is_test:
            # Admin test checkout — skip the brief intake and fulfill the kit
            # immediately using the canned QA brief carried in metadata. Leaving
            # pending_intake_id=None also suppresses the brief-intake email below.
            rfp_desc = (metadata.get("rfp_description") or "").strip()
            tasks_to_queue.append(("rfp", (rfp_product, rfp_desc)))
            logger.info(
                f"[Bundle:{product_type}] test_simulation — fulfilling {rfp_product} "
                f"directly (no intake) for {customer_email}"
            )
        elif rfp_product and owner_id:
            pending = PendingRfpIntake(
                user_id=owner_id,
                session_id=session_id,
                rfp_product_type=rfp_product,
                bundle_source=product_type,
                vendor_url=website or None,
                company_name=company_name or None,
                status="pending",
            )
            db.add(pending)
            db.flush()
            pending_intake_id = str(pending.id)
            logger.info(
                f"[Bundle:{product_type}] Created PendingRfpIntake {pending_intake_id} for {customer_email}"
            )
        elif rfp_product:
            logger.warning(
                f"[Bundle:{product_type}] RFP component skipped — no user resolved "
                f"for email={customer_email}; nothing to attach intake to"
            )

        # 3. Notarization credits — grant balance to user, no auto-fulfillment.
        # User redeems credits later by uploading documents at /notarize.
        notarization_count = components.get("notarization_count", 0)
        if notarization_count > 0 and customer_email:
            # Row-locked: webhook idempotency dedupes the same event_id, but
            # two *different* bundle purchases for the same email could land
            # near-simultaneously. Without the lock, both would read the
            # pre-grant balance and one increment would be lost.
            user = (
                db.query(User)
                .filter(User.email == customer_email)
                .with_for_update()
                .first()
            )
            if user:
                if product_type == "compliance_evidence_pack":
                    # CEP's 1 credit lives in a dedicated pool — it is reserved for
                    # the signed Cover Sheet upload at /compliance/cover-sheet.
                    # Does NOT accumulate: the workflow is exactly 1 signed sheet
                    # per cycle (one-time purchase OR per month for subscribers),
                    # so we normalise to 1 rather than stacking.
                    current_ce = getattr(user, "compliance_evidence_credits", 0) or 0
                    user.compliance_evidence_credits = max(current_ce, 1)
                    user.pending_cover_sheet = True
                    # Reset the lifetime "have you signed?" flag because this
                    # is a fresh cycle. Without this, a buyer who re-purchases
                    # CEP stays stuck on the post-sign UI from their prior
                    # cycle and never sees the new cycle's sign loop. The
                    # cycle-scoped `signed` payload (filtered by PDPA
                    # created_at) still surfaces the prior signed sheet for
                    # audit, just not as "you have signed THIS cycle".
                    user.signed_cover_sheet_uploaded = False
                    logger.info(
                        f"[Bundle:compliance_evidence_pack] Set CE credit=1 for {customer_email} "
                        f"(was {current_ce}); reset signed_cover_sheet_uploaded=False for fresh cycle"
                    )
                else:
                    current_balance = getattr(user, "notarization_credits", 0) or 0
                    user.notarization_credits = current_balance + notarization_count
                    logger.info(
                        f"[Bundle:{product_type}] Granted {notarization_count} notarization credits "
                        f"to {customer_email} (balance: {current_balance} → {user.notarization_credits})"
                    )
            else:
                await _alert_payment_fulfillment_issue(
                    reason=f"bundle paid but cannot grant {notarization_count} notarization credits — user disappeared between queries",
                    product_type=product_type,
                    customer_email=customer_email,
                    session_id=session_id,
                    extra={"credits_intended": notarization_count},
                )

        # Commit stubs + credit grant atomically before queuing tasks
        db.commit()

        # Now queue tasks — stubs are safely persisted
        for task_type, payload in tasks_to_queue:
            if task_type == "vendor_proof":
                fulfill_vendor_proof_task.delay(payload, customer_email)
                logger.info(
                    f"[Bundle:{product_type}] Queued vendor_proof for report {payload}"
                )
            elif task_type == "pdpa":
                fulfill_pdpa_task.delay(payload, customer_email)
                logger.info(f"[Bundle:{product_type}] Queued pdpa for report {payload}")
            elif task_type == "rfp":
                # Only reached for admin test checkouts — real purchases defer to
                # /rfp-intake via the PendingRfpIntake row created above.
                from app.workers.tasks import fulfill_rfp_task

                rfp_product, rfp_desc = payload
                fulfill_rfp_task.delay(
                    product_type=rfp_product,
                    vendor_id=str(owner_id),
                    vendor_email=customer_email or "",
                    vendor_url=website or "https://booppa.io",
                    company_name=company_name or "Booppa QA",
                    rfp_description=rfp_desc,
                    session_id=session_id,
                    intake_data=None,
                    # This branch is admin test checkout only — ship a kit even
                    # when the canned brief is thin/empty (don't block on
                    # residual placeholders), so the e2e test yields an RFP.
                    allow_incomplete=True,
                )
                logger.info(
                    f"[Bundle:{product_type}] Queued fulfill_rfp_task ({rfp_product}) "
                    f"for {customer_email} (test_simulation)"
                )

        # ── Single consolidated bundle email ────────────────────────────────
        # Previously this sent up to TWO separate purchase-time emails (a
        # notarization-credits email + an RFP brief-intake email), on top of the
        # async PDPA/RFP/cover-sheet deliverable emails — the inbox spam the
        # forensic audit flagged. We now compose ONE email that lists everything
        # the bundle includes + the single required next step. The component
        # deliverables (PDPA report, RFP kit, signed cover sheet) still email as
        # each completes, since they arrive at different times.
        sections: list[str] = []

        # Redeemable notarization credits — NOT for compliance_evidence_pack,
        # whose single credit is reserved for the Cover Sheet signing flow (which
        # has its own email when the sheet is ready), not /notarize redemption.
        if notarization_count > 0 and product_type != "compliance_evidence_pack":
            sections.append(f"""
                      <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:16px;margin:16px 0;">
                        <p style="margin:0 0 6px;font-weight:bold;color:#0369a1;">{notarization_count} notarization{'s' if notarization_count != 1 else ''} included</p>
                        <p style="margin:0;color:#334155;font-size:14px;">
                          Anchor any compliance document on-chain with SHA-256 proof at
                          <a href="https://www.booppa.io/notarize" style="color:#0ea5e9;font-weight:bold;">booppa.io/notarize</a>
                          (enter {customer_email} — credits apply automatically, no payment). Credits don't expire.
                        </p>
                      </div>""")

        if components.get("pdpa"):
            sections.append("""
                      <p style="color:#334155;font-size:14px;">📄 Your <strong>PDPA Snapshot</strong> scan is running now — the report arrives by email shortly.</p>""")

        # RFP Complete kit is part of the Compliance Evidence Pack. Always
        # announce it; the wording differs by path. When an RFP brief is still
        # outstanding (real purchase) the kit can't generate until the buyer
        # completes the brief — the CTA for that renders below. On the test/auto
        # path it generates straight away.
        if product_type == "compliance_evidence_pack" and components.get("rfp"):
            if pending_intake_id:
                sections.append("""
                      <p style="color:#334155;font-size:14px;">📑 Your <strong>RFP Complete kit</strong> is included — complete the short brief below to generate the GeBIZ-ready kit.</p>""")
            else:
                sections.append("""
                      <p style="color:#334155;font-size:14px;">📑 Your <strong>RFP Complete kit</strong> is being generated — the GeBIZ-ready kit arrives by email shortly.</p>""")

        # BCEP 7-document PDPA governance pack — announced on EVERY path. On a real
        # purchase the buyer must complete the structured intake first (CTA), so it
        # is intake_pending. On the test/cycle path it auto-queues (status queued/
        # ready) and generates without a brief — announce that it's on its way so
        # the deliverable is never silently dropped.
        if (
            product_type == "compliance_evidence_pack"
            and evidence_pack_row is not None
            and customer_email
        ):
            ep_status = getattr(evidence_pack_row, "status", None)
            if ep_status == "intake_pending":
                ep_intake_url = f"https://www.booppa.io/evidence-pack-intake/{evidence_pack_row.id}"
                sections.append(f"""
                      <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:16px 0;">
                        <p style="margin:0 0 8px;font-weight:bold;color:#065f46;">Start your PDPA Evidence Pack (7 documents)</p>
                        <p style="margin:0 0 12px;color:#334155;font-size:14px;">Complete a short structured intake (about 5 minutes) — org details, DPO, systems, data types — and we'll generate your DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook, Training Register, and Security Review Log.</p>
                        <a href="{ep_intake_url}" style="display:inline-block;background:#10b981;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:bold;">Complete your intake →</a>
                      </div>""")
            else:
                sections.append("""
                      <p style="color:#334155;font-size:14px;">📚 Your <strong>7-document PDPA Evidence Pack</strong> (DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook, Training Register, Security Review Log) is being generated — it arrives by email shortly.</p>""")

        # Compliance Cover Sheet — the centerpiece of the pack. It fires once the
        # PDPA Snapshot, RFP Complete kit, and the 7-document pack are all ready,
        # then indexes every one of them (see `_maybe_fire_cover_sheet`).
        if product_type == "compliance_evidence_pack" and components.get("cover_sheet"):
            sections.append("""
                      <p style="color:#334155;font-size:14px;">🛡️ Your signed <strong>Compliance Cover Sheet</strong> is emailed once every component above finishes — it indexes all of them into one blockchain-anchored evidence sheet.</p>""")

        # The RFP brief CTA is the one required action — it gates the RFP kit.
        brief_cta = ""
        if pending_intake_id and customer_email:
            kit_label = "RFP Complete Kit" if rfp_product == "rfp_complete" else "RFP Express Kit"
            intake_url = f"https://www.booppa.io/rfp-intake/{pending_intake_id}"
            brief_cta = f"""
                      <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:16px;margin:16px 0;">
                        <p style="margin:0 0 8px;font-weight:bold;color:#92400e;">One step to unlock your {kit_label}</p>
                        <p style="margin:0 0 12px;color:#334155;font-size:14px;">Share a few details about the procurement (about 2 minutes) and we'll generate the kit.</p>
                        <a href="{intake_url}" style="display:inline-block;background:#0ea5e9;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:bold;">Complete your RFP brief →</a>
                      </div>"""

        if (sections or brief_cta) and customer_email:
            bundle_label = product_type.replace('_', ' ').title()
            try:
                sent = await EmailService().send_html_email(
                    to_email=customer_email,
                    subject=f"Your {bundle_label} — what's included & next steps",
                    body_html=f"""
                    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
                      <h2 style="color:#0f172a;">Your {bundle_label} is being prepared</h2>
                      <p style="color:#334155;">Here's everything included and what happens next:</p>
                      {''.join(sections)}
                      {brief_cta}
                      <p style="color:#64748b;font-size:13px;margin-top:20px;">booppa.io</p>
                    </div>""",
                )
                if not sent:
                    logger.error(
                        f"[Bundle:{product_type}] Consolidated bundle email rejected for {customer_email}"
                    )
                    # A rejected email is critical when it carries the RFP brief
                    # CTA — without it the buyer can't unlock the kit they paid for.
                    if pending_intake_id:
                        await _alert_payment_fulfillment_issue(
                            reason="Bundle email (with RFP brief CTA) rejected by email provider",
                            product_type=product_type,
                            customer_email=customer_email,
                            session_id=session_id,
                            extra={"intake_id": pending_intake_id},
                            notify_customer=False,
                        )
                else:
                    logger.info(
                        f"[Bundle:{product_type}] Sent consolidated bundle email to {customer_email} "
                        f"(intake_id={pending_intake_id})"
                    )
            except Exception as email_err:
                logger.warning(
                    f"[Bundle:{product_type}] Consolidated bundle email failed: {email_err}"
                )

        # 4. Cover Sheet — auto-fires once BOTH inputs are ready, not at purchase.
        # `pending_cover_sheet=True` was set above (credit-grant block); when the
        # PDPA scan and the RFP Complete kit each finish they call
        # `_maybe_fire_cover_sheet`, which queues `fulfill_cover_sheet_task` as
        # soon as both are done. The hourly `sweep_pending_cover_sheets` beat task
        # is a backstop that re-fires for any buyer whose inline trigger was
        # missed, so the 3-doc bundle always delivers its cover sheet. The signed
        # version (with the buyer's signature anchored) is regenerated later when
        # they upload it via their CE credit at /compliance/cover-sheet.
        if components.get("cover_sheet"):
            logger.info(
                f"[Bundle:{product_type}] Cover sheet will auto-fire when PDPA + RFP "
                f"complete (pending_cover_sheet=True); hourly sweep backstops misses"
            )

    except Exception as e:
        logger.exception(f"[Bundle] Fulfillment error for {product_type}: {e}")
        db.rollback()
        await _alert_payment_fulfillment_issue(
            reason=f"bundle fulfillment raised exception: {type(e).__name__}: {e}",
            product_type=product_type,
            customer_email=customer_email,
            session_id=session_id,
        )
    finally:
        db.close()


async def _fulfill_notarization(report_id: str, customer_email: str | None) -> None:
    """
    Lightweight notarization fulfillment:
    1. Anchor the original file SHA-256 to the blockchain
    2. Generate a proper notarization certificate PDF
    3. Upload to S3, set pipeline flags, send email
    """
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[Notarize] Report {report_id} not found")
            return

        assessment = (
            report.assessment_data if isinstance(report.assessment_data, dict) else {}
        )
        file_hash = assessment.get("file_hash") or report.audit_hash
        original_filename = assessment.get("original_filename", "document")
        file_size = assessment.get("file_size_bytes")
        hash_algorithm = assessment.get("hash_algorithm", "SHA-256")
        mime_type = assessment.get("mime_type")
        document_descriptor = assessment.get("document_descriptor")
        contact_email = (
            customer_email
            or assessment.get("contact_email")
            or assessment.get("customer_email")
        )

        # Step 1: Anchor file hash on blockchain
        tx_hash = None
        try:
            blockchain = BlockchainService()
            tx_hash = await blockchain.anchor_evidence(
                file_hash, metadata=f"notarization:{report_id}"
            )
            # Only store a real hex tx_hash; None means already anchored (no new tx)
            if tx_hash:
                report.tx_hash = tx_hash
            else:
                # Hash already on-chain (e.g. duplicate file upload). Inherit the
                # tx_hash from the prior report that anchored this same hash so
                # this report is correctly counted as anchored and the receipt
                # email/PDF can include a Polygonscan link.
                prior = (
                    db.query(Report)
                    .filter(
                        Report.audit_hash == file_hash,
                        Report.id != report_id,
                        Report.tx_hash.isnot(None),
                        Report.tx_hash != "already_anchored",
                    )
                    .order_by(Report.created_at.asc())
                    .first()
                )
                if prior and prior.tx_hash:
                    tx_hash = prior.tx_hash
                    report.tx_hash = tx_hash
                    logger.info(
                        f"[Notarize] Hash already anchored — inherited tx={tx_hash} "
                        f"from prior report {prior.id}"
                    )
            report.audit_hash = file_hash  # keep as original file hash for verification
            assessment["blockchain_anchored"] = True
            assessment["blockchain_anchored_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            report.assessment_data = assessment
            flag_modified(report, "assessment_data")
            db.commit()
            logger.info(f"[Notarize] Anchored {file_hash[:16]}… tx={tx_hash}")
        except Exception as e:
            logger.error(f"[Notarize] Blockchain anchor failed for {report_id}: {e}")

        # Step 2: Build verify URL
        verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{file_hash}"
        polygonscan_url = (
            f"{settings.active_polygon_explorer_url.rstrip('/')}/tx/{tx_hash}"
            if tx_hash
            else None
        )

        # Step 3: Generate notarization certificate PDF
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            pdf_data = {
                "report_id": report_id,
                "framework": "compliance_notarization",
                "company_name": report.company_name,
                "created_at": (
                    report.created_at.isoformat()
                    if report.created_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                "status": "completed",
                "tx_hash": tx_hash,
                "audit_hash": file_hash,
                "original_filename": original_filename,
                "file_size": file_size,
                "hash_algorithm": hash_algorithm,
                "mime_type": mime_type,
                "document_descriptor": document_descriptor,
                "verify_url": verify_url,
                "polygonscan_url": polygonscan_url,
                "proof_header": "BOOPPA-PROOF-SG",
                "schema_version": "1.0",
                "network": settings.active_polygon_network_name,
                "testnet_notice": settings.blockchain_notice,
                "payment_confirmed": True,
                "tier": "pro",
                "contact_email": contact_email,
                "base_url": "https://www.booppa.io",
            }
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
            assessment["pdf_generated"] = True
            assessment["pdf_generated_at"] = datetime.now(timezone.utc).isoformat()
            report.assessment_data = assessment
            flag_modified(report, "assessment_data")
            db.commit()
        except Exception as e:
            logger.error(f"[Notarize] PDF generation failed for {report_id}: {e}")

        # Step 4: Upload PDF to S3
        pdf_url = None
        if pdf_bytes:
            try:
                storage = S3Service()
                pdf_url = await storage.upload_pdf(pdf_bytes, report_id)
                report.s3_url = pdf_url
                assessment["s3_uploaded"] = True
                assessment["s3_uploaded_at"] = datetime.now(timezone.utc).isoformat()
                assessment["verify_url"] = verify_url
                assessment["polygonscan_url"] = polygonscan_url
                report.assessment_data = assessment
                flag_modified(report, "assessment_data")
                db.commit()
            except Exception as e:
                logger.error(f"[Notarize] S3 upload failed for {report_id}: {e}")

        # Step 5: Mark completed
        report.status = "completed"
        report.completed_at = datetime.now(timezone.utc)
        db.commit()

        # Step 6: Send email (guard against duplicate sends on retry)
        already_emailed = assessment.get("notarization_email_sent")
        if contact_email and not already_emailed:
            try:
                email_svc = EmailService()
                download_section = (
                    f'<p><a href="{pdf_url}" style="background-color:#10b981;color:#fff;'
                    f'padding:10px 24px;text-decoration:none;border-radius:6px;font-weight:bold;">'
                    f"Download Notarization Certificate (PDF)</a></p>"
                    if pdf_url
                    else "<p>Your certificate will be available on the BOOPPA website once processing is complete.</p>"
                )
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;">
                  <h2 style="color:#10b981;">Your Notarization Certificate is Ready</h2>
                  <p>Hello {report.company_name or "Customer"},</p>
                  <p>Your blockchain notarization certificate for
                     <strong>{original_filename}</strong> has been generated.</p>
                  <p><strong>SHA-256 Hash:</strong> <code>{file_hash}</code></p>
                  {'<p><strong>Blockchain TX:</strong> <a href="' + polygonscan_url + '">' + (tx_hash or '') + '</a></p>' if polygonscan_url else ''}
                  {download_section}
                  <p style="color:#64748b;font-size:12px;">
                    Certificate ID: {report_id}<br>
                    Network: {settings.active_polygon_network_name}
                  </p>
                  <p>Thank you for using BOOPPA.</p>
                </body></html>
                """
                sent = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Notarization Certificate is Ready — {original_filename}",
                    body_html=body_html,
                    attachments=[(f"Notarization_{report_id[:8]}.pdf", pdf_bytes)] if pdf_bytes else None,
                )
                if sent:
                    # Mark email as sent to prevent duplicates on retry. Only on a
                    # confirmed send — a False return must leave the flag unset so
                    # the retry path can re-deliver the certificate.
                    assessment["notarization_email_sent"] = True
                    assessment["notarization_email_sent_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    report.assessment_data = assessment
                    flag_modified(report, "assessment_data")
                    db.commit()
                else:
                    await _alert_payment_fulfillment_issue(
                        reason="notarization certificate email rejected by provider",
                        product_type="notarization",
                        customer_email=contact_email,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[Notarize] Email failed for {report_id}: {e}")

        # Step 7: Update elevation metadata so CAL advances to NOTARIZED
        try:
            from app.services.notarization_elevation import create_or_update_elevation
            from app.core.models import User
            from app.core.models_v6 import VerifyRecord, Proof

            # Resolve real user from contact_email (report.owner_id may be a random UUID
            # if the report was created via the public unauthenticated endpoint)
            real_vendor_id = None
            if contact_email:
                real_user = db.query(User).filter(User.email == contact_email).first()
                if real_user:
                    real_vendor_id = str(real_user.id)
                    # Also remap the report so future lookups are consistent
                    if str(report.owner_id) != real_vendor_id:
                        report.owner_id = real_user.id
                        db.flush()

            vendor_id = real_vendor_id or str(report.owner_id)

            # create_or_update_elevation counts Proof records linked to the vendor's
            # VerifyRecord. The notarization flow doesn't create Proof rows, so we
            # create one now to represent this completed notarization.
            verify = (
                db.query(VerifyRecord)
                .filter(VerifyRecord.vendor_id == vendor_id)
                .first()
            )
            if verify and file_hash:
                existing_proof = (
                    db.query(Proof)
                    .filter(
                        Proof.verify_id == verify.id,
                        Proof.hash_value == file_hash,
                    )
                    .first()
                )
                if not existing_proof:
                    proof = Proof(
                        verify_id=verify.id,
                        hash_value=file_hash,
                        title=original_filename or "Notarized Document",
                        compliance_score=5,
                        metadata_json={
                            "report_id": report_id,
                            "tx_hash": tx_hash,
                            "notarized_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    db.add(proof)
                    db.commit()
                    logger.info(
                        f"[Notarize] Created Proof record for vendor {vendor_id}"
                    )

            create_or_update_elevation(db, vendor_id)
            logger.info(f"[Notarize] Elevation metadata updated for vendor {vendor_id}")

            # Sync VerifyRecord.verification_level to match the new depth so the
            # compliance score weight (1.0×BASIC → 1.1×STANDARD) updates correctly.
            try:
                from app.core.models_v6 import (
                    VerifyRecord as _VR,
                    VerificationLevel as _VL,
                )
                from app.services.vendor_status import compute_verification_depth

                _new_depth = compute_verification_depth(db, vendor_id)
                _depth_to_level = {
                    "STANDARD": _VL.STANDARD,
                    "DEEP": _VL.PREMIUM,
                    "CERTIFIED": _VL.GOVERNMENT,
                    "ENTERPRISE": _VL.GOVERNMENT,
                }
                _new_level = _depth_to_level.get(_new_depth)
                if _new_level:
                    _vr = db.query(_VR).filter(_VR.vendor_id == vendor_id).first()
                    if _vr and _vr.verification_level != _new_level:
                        _vr.verification_level = _new_level
                        db.commit()
                        logger.info(
                            f"[Notarize] VerifyRecord.verification_level → {_new_level.value} for {vendor_id}"
                        )
            except Exception as lvl_err:
                logger.warning(
                    f"[Notarize] verification_level sync failed for {vendor_id}: {lvl_err}"
                )

            # Record fulfillment so Engagement + Recency move on each notarization.
            _log_purchase_activity(
                db,
                vendor_id,
                activity_type="NOTARIZATION_FULFILLED",
                description=f"Notarization fulfilled: report={report_id}",
                extra={"report_id": str(report_id), "tx_hash": tx_hash},
            )

            # Recalculate compliance score so the dashboard reflects the new document.
            try:
                from app.services.scoring import VendorScoreEngine

                VendorScoreEngine.update_vendor_score(db, vendor_id)
                logger.info(f"[Notarize] Vendor score recalculated for {vendor_id}")
            except Exception as score_err:
                logger.warning(
                    f"[Notarize] Score update failed for {vendor_id}: {score_err}"
                )

            # Refresh the procurement snapshot so tender win probability
            # reflects the newly created Proof and elevation data.
            try:
                from app.services.vendor_status import upsert_status_snapshot

                upsert_status_snapshot(db, vendor_id)
                logger.info(
                    f"[Notarize] Status snapshot refreshed for vendor {vendor_id}"
                )
            except Exception as snap_err:
                logger.warning(
                    f"[Notarize] Snapshot refresh failed for {vendor_id}: {snap_err}"
                )
        except Exception as e:
            logger.warning(f"[Notarize] Elevation update failed for {report_id}: {e}")

        logger.info(f"[Notarize] Fulfilled {report_id}: tx={tx_hash} pdf={pdf_url}")
        _maybe_fire_cover_sheet(contact_email)
    except Exception as e:
        logger.error(f"[Notarize] Fulfillment error for {report_id}: {e}")
    finally:
        db.close()


class _RfpDeliverableIncomplete(Exception):
    """Raised when an RFP kit built successfully but a tier-defining deliverable
    (the editable DOCX for rfp_complete) is missing. Propagated past the
    swallow-all handler so ``fulfill_rfp_task`` retries instead of shipping a
    PDF-only kit for the SGD 599 Complete tier."""


async def _fulfill_rfp_package(
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
    session_id: str | None = None,
    intake_data: dict | None = None,
    allow_incomplete: bool = False,
) -> None:
    """Background task: generate and deliver the RFP Kit package after payment."""
    db = SessionLocal()
    try:
        from app.services.rfp_express_builder import RFPExpressBuilder

        rfp_details = {"description": rfp_description} if rfp_description else None
        if intake_data:
            rfp_details = {**(rfp_details or {}), "intake": intake_data}
        builder = RFPExpressBuilder(
            vendor_id=vendor_id, vendor_email=vendor_email, session_id=session_id
        )
        result = await builder.generate_express_package(
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_details=rfp_details,
            db=db,
            product_type=product_type,
            allow_incomplete=allow_incomplete,
        )
        # HARD GATE (audit fix): the builder blocks delivery when any
        # [Verify:]/[FILL IN] placeholder survives intake substitution. Do NOT
        # persist a completed Report, do NOT cache an rfp_result — instead route
        # the buyer back to the intake form to supply the missing facts, then
        # they resubmit and we regenerate.
        if result.get("blocked"):
            await _handle_blocked_rfp(
                db=db,
                product_type=product_type,
                vendor_email=vendor_email,
                session_id=session_id,
                missing_fields=result.get("missing_fields") or [],
                residual_placeholders=result.get("residual_placeholders") or 0,
                company_name=company_name,
            )
            return

        download_url = result.get("download_url")
        logger.info(
            f"RFP package fulfilled: product={product_type} vendor={vendor_id} "
            f"url={download_url} errors={result.get('errors')}"
        )

        # Complete-tier hard deliverable gate (audit fix). The editable DOCX is
        # the defining extra of rfp_complete (SGD 599 vs SGD 249 Express). If it
        # failed to build or upload, `docx_url` comes back None and the kit would
        # previously ship PDF-only, silently — the buyer pays for the Complete
        # tier and never gets its distinguishing deliverable. Refuse to persist /
        # cache / email a PDF-only kit: alert support and raise so the Celery
        # task retries. The test/admin-sim path (allow_incomplete) is exempt so
        # the e2e harness still yields a kit without a live S3 bucket.
        if (
            product_type == "rfp_complete"
            and not allow_incomplete
            and not result.get("docx_url")
        ):
            logger.error(
                "[RFP] rfp_complete for %s produced no docx_url — refusing PDF-only "
                "delivery; will retry. errors=%s warnings=%s",
                vendor_email, result.get("errors"), result.get("warnings"),
            )
            try:
                await _alert_payment_fulfillment_issue(
                    reason="RFP Complete kit generated without the editable DOCX deliverable",
                    product_type=product_type,
                    customer_email=vendor_email,
                    session_id=session_id,
                    notify_customer=False,
                )
            except Exception as _ae:
                logger.warning("[RFP] docx-missing alert failed: %s", _ae)
            raise _RfpDeliverableIncomplete(
                f"rfp_complete missing docx_url (session={session_id})"
            )

        # Persist a Report row for every completed RFP so the bundle progress
        # page (and any future audit query) can find it. The cover-sheet
        # auto-fire is still gated on pending_cover_sheet below.
        if vendor_email:
            try:
                ce_user = db.query(User).filter(User.email == vendor_email).first()
                if ce_user:
                    from datetime import datetime as _dt, timezone as _tz

                    rfp_report = Report(
                        owner_id=ce_user.id,
                        framework="rfp_complete",
                        company_name=company_name or "Your Organisation",
                        company_website=vendor_url,
                        assessment_data={
                            "product_type": product_type,
                            "download_url": download_url,
                            "s3_key": result.get("pdf_s3_key"),
                            "docx_url": result.get("docx_url"),
                            "declaration_url": result.get("declaration_url"),
                            "appendix_d_url": result.get("appendix_d_url"),
                            # Persist the full Q&A list (not just the count) so
                            # the Compliance Cover Sheet can embed it later —
                            # the result cache expires, the Report row doesn't.
                            "qa_answers": result.get("qa_answers", []) or [],
                            "qa_count": len(result.get("qa_answers", []) or []),
                            "answer_source": result.get("answer_source"),
                            "discrepancies": result.get("discrepancies") or [],
                            "data_sources": result.get("data_sources") or {},
                            "generated_at": result.get("generated_at"),
                            "polygonscan_url": result.get("polygonscan_url"),
                            # Persist the buyer's confirmed inputs so monthly
                            # refresh emails (compliance_evidence_monthly) can
                            # pre-fill /rfp-intake/{new_id} with last month's
                            # answers — 30-second re-confirm instead of full re-entry.
                            "intake_rfp_description": rfp_description or "",
                            "intake_data": intake_data or {},
                        },
                        status="completed",
                        tx_hash=result.get("tx_hash"),
                        completed_at=_dt.now(_tz.utc),
                    )
                    db.add(rfp_report)
                    if getattr(ce_user, "pending_cover_sheet", False):
                        ce_user.compliance_evidence_rfp_ready = True
                    db.commit()
                    if getattr(ce_user, "pending_cover_sheet", False):
                        _maybe_fire_cover_sheet(vendor_email)
            except Exception as flag_err:
                logger.warning(
                    f"[RFP→CoverSheet] Could not record RFP completion for {vendor_email}: {flag_err}"
                )
        # Store result keyed by session_id so the result page can retrieve it
        if session_id and download_url:
            from app.core.cache import cache as cache_mod

            cache_mod.set(
                cache_mod.cache_key(f"rfp_result:{session_id}"),
                {
                    "download_url": download_url,
                    "docx_url": result.get("docx_url"),
                    "declaration_url": result.get("declaration_url"),
                    "appendix_d_url": result.get("appendix_d_url"),
                    "product_type": product_type,
                    "company_name": company_name,
                    "vendor_url": vendor_url,
                    "qa_answers": result.get("qa_answers", []),
                    "tx_hash": result.get("tx_hash"),
                    "polygonscan_url": result.get("polygonscan_url"),
                    "generated_at": result.get("generated_at"),
                    "expires_at": result.get("expires_at"),
                    "data_sources": result.get("data_sources", {}),
                    "discrepancies": result.get("discrepancies", []),
                    "warnings": result.get("warnings", []),
                    "answer_source": result.get("answer_source", "ai_grounded"),
                },
                ttl=604800,  # 7 days
            )
    except _RfpDeliverableIncomplete:
        # Re-raise so fulfill_rfp_task's retry/backoff kicks in — do NOT swallow
        # into a success like the generic handler below.
        raise
    except Exception as e:
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {e}")
    finally:
        db.close()


async def _handle_blocked_rfp(
    db,
    product_type: str,
    vendor_email: str,
    session_id: str | None,
    missing_fields: list[str],
    residual_placeholders: int,
    company_name: str,
) -> None:
    """An RFP kit was blocked at the hard placeholder gate. Route the buyer back
    to intake to supply the missing facts (status -> needs_more_info), email them
    the exact fields to complete, and alert ops. No kit is delivered or cached.
    """
    from app.core.models_v12 import PendingRfpIntake

    intake_id = None
    try:
        row = None
        if session_id:
            row = (
                db.query(PendingRfpIntake)
                .filter(PendingRfpIntake.session_id == session_id)
                .order_by(PendingRfpIntake.created_at.desc())
                .first()
            )
        if row is None and vendor_email:
            # Fall back to this buyer's most recent submitted/pending intake.
            buyer = db.query(User).filter(User.email == vendor_email).first()
            if buyer:
                row = (
                    db.query(PendingRfpIntake)
                    .filter(PendingRfpIntake.user_id == buyer.id)
                    .order_by(PendingRfpIntake.created_at.desc())
                    .first()
                )
        if row is not None:
            row.status = "needs_more_info"
            db.commit()
            intake_id = str(row.id)
    except Exception as flip_err:
        logger.warning(f"[RFP] Could not flip intake to needs_more_info: {flip_err}")

    # Email the buyer the precise fields to complete, with a link back to intake.
    try:
        import html as _html
        from app.services.email_service import EmailService

        intake_link = (
            f"https://www.booppa.io/rfp-intake/{intake_id}" if intake_id else None
        )
        fields_html = "".join(
            f"<li style=\"margin:4px 0;\">{_html.escape(f)}</li>" for f in missing_fields
        ) or "<li>Some verification details were missing.</li>"
        cta_html = (
            f'<p style="text-align:center;margin:24px 0;"><a href="{intake_link}" '
            f'style="background:#10b981;color:#fff;padding:12px 28px;text-decoration:none;'
            f'border-radius:8px;font-weight:bold;display:inline-block;">Complete your RFP brief</a></p>'
            if intake_link else
            '<p>Please return to your RFP brief on booppa.io to complete the missing details.</p>'
        )
        body_html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
          <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
            <h1 style="color:#10b981;margin:0;font-size:20px;">A few details needed to finish your RFP Kit</h1>
          </div>
          <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
            <p>Hello <strong>{_html.escape(company_name or "there")}</strong>,</p>
            <p>Your RFP Complete Kit is almost ready. To make it usable for a real GeBIZ tender, we
               need you to confirm a few verification details we could not source automatically.
               Your kit will be generated and delivered as soon as you complete these:</p>
            <ul style="font-size:14px;color:#334155;padding-left:20px;">{fields_html}</ul>
            {cta_html}
            <p style="color:#64748b;font-size:12px;">We don't deliver kits with unverified
               placeholders — GeBIZ procurement officers reject them. This step keeps yours submission-ready.</p>
          </div>
        </body></html>"""
        sent = await EmailService().send_html_email(
            to_email=vendor_email,
            subject="Action needed: complete your RFP Kit details",
            body_html=body_html,
        )
        if not sent:
            logger.error(f"[RFP] needs-more-info email rejected for {vendor_email}")
    except Exception as mail_err:
        logger.warning(f"[RFP] Could not send needs-more-info email: {mail_err}")

    # Ops visibility.
    try:
        await _alert_payment_fulfillment_issue(
            reason=(
                f"RFP kit BLOCKED at hard placeholder gate — {residual_placeholders} "
                "unfilled [Verify:]/[FILL IN] field(s); buyer routed back to intake"
            ),
            product_type=product_type,
            customer_email=vendor_email,
            session_id=session_id,
            extra={
                "residual_placeholders": residual_placeholders,
                "missing_fields": missing_fields,
                "company_name": company_name,
                "intake_id": intake_id,
            },
            notify_customer=False,
        )
    except Exception as alert_err:
        logger.warning(f"[RFP] Block alert failed (non-blocking): {alert_err}")


def _maybe_fire_cover_sheet(customer_email: str | None) -> None:
    """
    Auto-fire the Compliance Evidence Pack cover sheet once ALL of its inputs
    have finished. The cover sheet is the centerpiece of the pack: it indexes
    every deliverable, so it must wait for all three auto-generated components —
    the PDPA Snapshot, the RFP Complete kit, AND the BCEP 7-document governance
    pack (`EvidencePack` `status=="ready"`, folded into DOCUMENTS ANCHORED by
    `fulfill_cover_sheet_task`). The user then signs the emailed cover sheet PDF
    and uploads it via their 1 included notarization credit.

    Notarization is intentionally NOT a precondition here: the cover sheet
    must reach the user *before* they consume the credit, otherwise they
    have nothing to sign and notarize.

    Backstop: a buyer who never completes the evidence-pack intake would block
    their cover sheet forever. If PDPA + RFP have been ready for more than
    `_COVER_SHEET_BCEP_GRACE_DAYS` and the pack still isn't ready, fire the sheet
    anyway (the BCEP-folding block degrades gracefully to PDPA + RFP only).

    Idempotent — clears `pending_cover_sheet` once queued so duplicate calls
    (any component finishing after another) don't re-fire.
    """
    if not customer_email:
        return
    db = SessionLocal()
    try:
        # Lock the user row so two concurrent callers (components completing
        # near-simultaneously) can't both pass the pending_cover_sheet check
        # and queue the task twice. The loser blocks until the winner commits
        # the False flip, then exits at the guard below.
        user = (
            db.query(User)
            .filter(User.email == customer_email)
            .with_for_update()
            .first()
        )
        if not user or not getattr(user, "pending_cover_sheet", False):
            db.commit()
            return

        # Cover-sheet readiness must reflect a *deliverable* PDPA scan, using
        # the same guard the render path applies (forensic finding: an
        # empty-score artifact — "Vendor: Test", suite-b.booppa.io, all scores
        # "—" — was bundled into a paying customer's pack). Take the newest
        # completed scan that has a real, resolvable score — not the oldest row,
        # and not a stub / empty-score scan the render path would then reject.
        from app.services.pdpa_findings import resolve_pdpa_score as _resolve_pdpa_score

        _pdpa_candidates = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .limit(10)
            .all()
        )
        pdpa_report = None
        for _cand in _pdpa_candidates:
            _cad = _cand.assessment_data if isinstance(_cand.assessment_data, dict) else {}
            if _resolve_pdpa_score(_cad) is None:
                continue  # empty-score scan — not a deliverable
            pdpa_report = _cand
            break
        pdpa_done = pdpa_report is not None
        rfp_done = bool(getattr(user, "compliance_evidence_rfp_ready", False))
        if not (pdpa_done and rfp_done):
            db.commit()
            return

        # The BCEP 7-document pack is the third input. It generates only after
        # the buyer completes the (separate) evidence-pack intake, so it is
        # usually the last to finish — wait for it unless the grace window has
        # elapsed (buyer never completed the intake), so nobody is left without
        # a cover sheet.
        from app.core.models_v13 import EvidencePack

        # Only *wait* when a non-ready pack row actually exists. A buyer with no
        # pack row at all is not owed a 7-doc pack (nothing is coming), so the
        # sheet fires immediately — same as before this change.
        latest_pack = (
            db.query(EvidencePack)
            .filter(EvidencePack.user_id == user.id)
            .order_by(EvidencePack.created_at.desc())
            .first()
        )
        bcep_pending = latest_pack is not None and latest_pack.status != "ready"
        if bcep_pending:
            pdpa_age_ok = False
            if pdpa_report is not None and pdpa_report.created_at is not None:
                created = pdpa_report.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                pdpa_age_ok = (
                    datetime.now(timezone.utc) - created
                ) > timedelta(days=_COVER_SHEET_BCEP_GRACE_DAYS)
            if not pdpa_age_ok:
                # Pack still pending and within grace — leave pending_cover_sheet
                # set so the hourly sweep / the pack's own completion re-checks.
                db.commit()
                return
            logger.info(
                "[CoverSheet] BCEP pack still not ready after %d-day grace for %s "
                "— firing cover sheet with PDPA + RFP only",
                _COVER_SHEET_BCEP_GRACE_DAYS,
                customer_email,
            )

        user.pending_cover_sheet = False
        db.commit()
        company_name = (user.company or "").strip() or "Your Organisation"
    finally:
        db.close()

    try:
        from app.workers.tasks import fulfill_cover_sheet_task

        fulfill_cover_sheet_task.apply_async(
            kwargs={
                "bundle_type": "compliance_evidence_pack",
                "customer_email": customer_email,
                "company_name": company_name,
                "metadata": {"auto_fired": True},
            },
            countdown=10,
        )
        logger.info(
            f"[CoverSheet] Auto-fired for {customer_email} (all components ready)"
        )
    except Exception as e:
        logger.warning(f"[CoverSheet] Auto-fire failed for {customer_email}: {e}")


async def _fulfill_vendor_proof(report_id: str, customer_email: str | None) -> None:
    """
    Vendor Proof fulfillment:
    1. Create/upsert VerifyRecord (lifecycleStatus=ACTIVE, complianceScore=30)
    2. Create/upsert VendorStatusSnapshot (verificationDepth=BASIC, procurementReadiness=CONDITIONAL)
    3. Create/upsert VendorScore baseline (complianceScore=30, totalScore=30)
    4. Send confirmation email with embeddable badge HTML
    """
    db = SessionLocal()
    try:
        from app.core.models_v6 import (
            VerifyRecord,
            LifecycleStatus,
            VerificationLevel,
            VendorScore,
            VendorSector,
        )
        from app.core.models_v8 import VendorStatusSnapshot

        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[VendorProof] Report {report_id} not found")
            return

        vendor_id = report.owner_id
        contact_email = customer_email or (report.assessment_data or {}).get(
            "contact_email"
        )

        # If the report was created via the public endpoint, owner_id is a random UUID.
        # Resolve the real user by email so VerifyRecord is linked to an actual account.
        if contact_email:
            from app.core.models import User

            real_user = db.query(User).filter(User.email == contact_email).first()
            if real_user:
                if str(vendor_id) != str(real_user.id):
                    logger.info(
                        f"[VendorProof] Remapping owner_id {vendor_id} → {real_user.id} "
                        f"via email={contact_email}"
                    )
                    vendor_id = real_user.id
                    report.owner_id = real_user.id
                    db.flush()

        if not vendor_id:
            logger.error(
                f"[VendorProof] No owner_id on report {report_id} and no user resolved from email"
            )
            return
        company_name = report.company_name or "Vendor"
        verify_url = f"https://www.booppa.io/verify/{report_id}"

        # ── Honest score, computed BEFORE we persist it ─────────────────────
        # Derive the standing from the vendor's ACTUAL latest PDPA scan when one
        # exists; otherwise it stays the identity-verified-only floor (30). This
        # value (vp_confidence) is the single source for VerifyRecord, the
        # snapshot, and VendorScore below — no more hardcoded 30s.
        from app.core.models import Report as _Report

        _latest_pdpa = (
            db.query(_Report)
            .filter(
                _Report.owner_id == vendor_id,
                _Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                _Report.status == "completed",
            )
            .order_by(_Report.created_at.desc())
            .first()
        )
        _pdpa_compliance = None
        if _latest_pdpa and isinstance(_latest_pdpa.assessment_data, dict):
            _cs = _latest_pdpa.assessment_data.get("compliance_score")
            if isinstance(_cs, (int, float)):
                _pdpa_compliance = int(round(_cs))

        if _pdpa_compliance is None:
            vp_readiness = "CONDITIONAL"
            vp_confidence = 30.0
            vp_readiness_label = "Identity verified — compliance not yet assessed"
            vp_score_display = "Not yet assessed — run a PDPA scan to establish it"
        elif _pdpa_compliance >= 70:
            vp_readiness = "READY"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Ready"
            vp_score_display = f"{_pdpa_compliance}/100"
        elif _pdpa_compliance >= 40:
            vp_readiness = "CONDITIONAL"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Conditional"
            vp_score_display = f"{_pdpa_compliance}/100"
        else:
            vp_readiness = "NOT_READY"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Action required — critical compliance gaps"
            vp_score_display = f"{_pdpa_compliance}/100"

        _vp_score_int = int(round(vp_confidence))

        # ── ACRA registry lookup (identity attestation) ─────────────────────
        # Match the buyer's UEN against the imported ACRA registry so the
        # certificate can state real registration details instead of nothing.
        acra_info: dict = {"matched": False}
        _uen = (report.assessment_data or {}).get("uen")
        if _uen:
            try:
                from app.core.models_v10 import DiscoveredVendor

                _dv = (
                    db.query(DiscoveredVendor)
                    .filter(DiscoveredVendor.uen == _uen)
                    .first()
                )
                if _dv:
                    acra_info = {
                        "matched": True,
                        "entity_type": _dv.entity_type,
                        "registration_date": _dv.registration_date,
                        "industry": _dv.industry,
                        "source": _dv.source,
                        "registry_company_name": _dv.company_name,
                    }
            except Exception as _acra_err:
                logger.warning("[VendorProof] ACRA lookup failed for UEN %s: %s", _uen, _acra_err)

            # Live data.gov.sg fallback — gives us the entity's current status
            # (LIVE / struck-off / ceased) and fills registration details when the
            # imported registry has no row. A non-live status is surfaced on the
            # certificate + verify page rather than blocking (the sale is already
            # paid by the time this webhook runs).
            try:
                from app.services.evidence_enricher import fetch_acra_status

                _live = await fetch_acra_status(_uen)
                if _live.get("found"):
                    acra_info.setdefault("entity_type", _live.get("entity_type"))
                    acra_info.setdefault("registration_date", _live.get("registration_date"))
                    acra_info["entity_status"] = _live.get("entity_status")
                    acra_info["entity_live"] = _live.get("live")
                    if not acra_info.get("matched"):
                        acra_info["matched"] = True
                        acra_info["source"] = "data.gov.sg (live)"
                        if _live.get("registered_name"):
                            acra_info["registry_company_name"] = _live.get("registered_name")
            except Exception as _live_err:
                logger.warning("[VendorProof] live ACRA lookup failed for UEN %s: %s", _uen, _live_err)

        # Step 1: Create or upsert VerifyRecord
        # Vendor Proof certificates are valid for 12 months; expiry drives the
        # renewal reminder (check_vendor_proof_expiry) and the active/expired
        # badge on the public verify page.
        _vp_expires_at = datetime.now(timezone.utc) + timedelta(days=365)
        verify = (
            db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
        )
        if verify:
            verify.lifecycle_status = LifecycleStatus.ACTIVE
            verify.compliance_score = _vp_score_int
            verify.verification_level = VerificationLevel.BASIC
            verify.last_refreshed_at = datetime.now(timezone.utc)
            verify.expires_at = _vp_expires_at
            verify.company_name = company_name
        else:
            verify = VerifyRecord(
                vendor_id=vendor_id,
                company_name=company_name,
                compliance_score=_vp_score_int,
                verification_level=VerificationLevel.BASIC,
                lifecycle_status=LifecycleStatus.ACTIVE,
                expires_at=_vp_expires_at,
                correlation_id=str(report_id),
            )
            db.add(verify)
        db.flush()

        # Step 1b: Seed VendorSector from report metadata or assessment data
        sector = (
            (report.assessment_data or {}).get("sector")
            or (report.assessment_data or {}).get("industry")
            or (report.assessment_data or {}).get("business_sector")
        )
        if sector:
            existing_sector = (
                db.query(VendorSector)
                .filter(
                    VendorSector.vendor_id == vendor_id,
                    VendorSector.sector == sector,
                )
                .first()
            )
            if not existing_sector:
                db.add(VendorSector(vendor_id=vendor_id, sector=sector))
                db.flush()

        # Step 2: Create or upsert VendorStatusSnapshot
        snapshot = (
            db.query(VendorStatusSnapshot)
            .filter(VendorStatusSnapshot.vendor_id == vendor_id)
            .first()
        )
        if snapshot:
            if snapshot.verification_depth in ("UNVERIFIED", None):
                snapshot.verification_depth = "BASIC"
            # Reflect actual standing — never silently upgrade NOT_READY → CONDITIONAL.
            snapshot.procurement_readiness = vp_readiness
            snapshot.confidence_score = vp_confidence
            snapshot.computed_at = datetime.now(timezone.utc)
        else:
            snapshot = VendorStatusSnapshot(
                vendor_id=vendor_id,
                verification_depth="BASIC",
                monitoring_activity="ACTIVE",
                risk_signal="CLEAN",
                procurement_readiness=vp_readiness,
                confidence_score=vp_confidence,
                evidence_count=0,
                notarization_depth=0,
                dual_silent_mode="SILENT_RISK_CAPTURE",
            )
            db.add(snapshot)

        # Step 3: Create or upsert VendorScore baseline
        score_row = (
            db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        )
        if score_row:
            if (score_row.compliance_score or 0) < _vp_score_int:
                score_row.compliance_score = _vp_score_int
            if (score_row.total_score or 0) < _vp_score_int:
                score_row.total_score = _vp_score_int
            score_row.updated_at = datetime.now(timezone.utc)
        else:
            score_row = VendorScore(
                vendor_id=vendor_id,
                compliance_score=_vp_score_int,
                total_score=_vp_score_int,
            )
            db.add(score_row)

        # Step 3b: Generate the Vendor Proof certificate PDF → S3 → anchor.
        # Non-fatal: a failure here still leaves the vendor verified; we just
        # don't attach a downloadable certificate.
        cert_url: str | None = None
        cert_tx_hash: str | None = None
        cert_pdf: bytes | None = None
        _vp_expires_display = _vp_expires_at.strftime("%d %B %Y")
        try:
            from app.services.vendor_proof_generator import generate_vendor_proof_certificate
            from app.services.storage import S3Service
            from app.core.models import User as _User
            import hashlib as _hashlib

            # Notarization credit balance at issue — surfaces the redemption line
            # on the certificate. Standalone Vendor Proof grants none; Vendor
            # Trust Pack grants 2 (already applied to the balance by webhook).
            _vp_credits = (
                db.query(_User.notarization_credits)
                .filter(_User.id == vendor_id)
                .scalar()
            ) or 0

            # Sector benchmark — position the Trust Score against same-sector peers
            # (falls back to all-vendors when the sector cohort is too thin, or
            # None when there aren't enough peers to benchmark at all).
            _vp_benchmark = None
            try:
                from app.services.vendor_benchmark import compute_sector_benchmark
                _vp_benchmark = compute_sector_benchmark(
                    db, vendor_id, _pdpa_compliance, sector
                )
            except Exception:
                logger.exception("[VendorProof] sector benchmark computation failed")

            cert_pdf = generate_vendor_proof_certificate(
                company_name=company_name,
                uen=_uen,
                acra_data=acra_info,
                score=(_pdpa_compliance if _pdpa_compliance is not None else "Identity verified only"),
                verification_level="BASIC",
                readiness_label=vp_readiness_label,
                verified_on=datetime.now(timezone.utc).strftime("%d %B %Y"),
                verify_url=verify_url,
                network_name=settings.active_polygon_network_name,
                explorer_url=settings.active_polygon_explorer_url,
                entity_status=acra_info.get("entity_status"),
                expires_on=_vp_expires_display,
                notarization_credits=int(_vp_credits),
                sector_benchmark=_vp_benchmark,
            )
            cert_hash = _hashlib.sha256(cert_pdf).hexdigest()

            s3 = S3Service()
            cert_report_id = f"vendor-proof-{report_id}"
            cert_url = await s3.upload_pdf(cert_pdf, cert_report_id)

            try:
                from app.services.blockchain import BlockchainService

                cert_tx_hash = await BlockchainService().anchor_evidence(
                    cert_hash, metadata=f"vendor_proof:{report_id}",
                )
            except Exception as _anchor_err:
                logger.warning("[VendorProof] Anchor failed for %s: %s", report_id, _anchor_err)

            report.s3_url = cert_url
            report.file_key = f"reports/{cert_report_id}.pdf"
            report.audit_hash = cert_hash
            if cert_tx_hash:
                report.tx_hash = cert_tx_hash
        except Exception as _cert_err:
            logger.error("[VendorProof] Certificate generation failed for %s: %s", report_id, _cert_err)

        # Mark report complete
        ad = report.assessment_data or {}
        ad["vendor_proof_fulfilled"] = True
        ad["verify_url"] = verify_url
        ad["compliance_score"] = _vp_score_int
        ad["procurement_readiness"] = vp_readiness
        ad["verification_level"] = "BASIC"
        ad["certificate_expires_at"] = _vp_expires_at.isoformat()
        ad["acra_verified"] = acra_info.get("matched", False)
        if acra_info.get("matched"):
            ad["acra_entity_type"] = acra_info.get("entity_type")
            ad["acra_registration_date"] = acra_info.get("registration_date")
        if acra_info.get("entity_status"):
            ad["acra_entity_status"] = acra_info.get("entity_status")
            ad["acra_entity_live"] = acra_info.get("entity_live")
        if cert_url:
            ad["certificate_url"] = cert_url
        if cert_tx_hash:
            ad["certificate_tx_hash"] = cert_tx_hash
        report.assessment_data = ad
        flag_modified(report, "assessment_data")
        report.status = "completed"
        report.completed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            f"[VendorProof] VerifyRecord + snapshot created for vendor {vendor_id}"
        )

        # Step 4: Seed ScoreSnapshot so monitoring shows ACTIVE immediately
        try:
            from app.services.scoring import VendorScoreEngine

            VendorScoreEngine.update_vendor_score(db, str(vendor_id))
        except Exception as e:
            logger.warning(f"[VendorProof] Score update failed for {vendor_id}: {e}")

        # Step 5: Email with embeddable badge
        if contact_email:
            # The badge attests IDENTITY/registration on BOOPPA — not compliance
            # approval. Wording is deliberately "Identity Verified" (not a bare
            # "Verified" that reads as a compliance pass) and the linked verify
            # page shows the real readiness, so a procurement officer is never
            # misled about a vendor with open compliance gaps (audit finding).
            badge_html = (
                f'<a href="{verify_url}" target="_blank" rel="noopener noreferrer" '
                f'style="display:inline-flex;align-items:center;gap:8px;background:#0f172a;'
                f"color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;"
                f'font-family:Arial,sans-serif;font-size:13px;font-weight:600;">'
                f'<span style="color:#10b981;">✓</span> {company_name} — Identity Verified on BOOPPA</a>'
            )
            body_html = f"""
            <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
              <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                <h1 style="color:#10b981;margin:0;font-size:20px;">Vendor Proof Activated</h1>
              </div>
              <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                <p>Hello <strong>{company_name}</strong>,</p>
                <p>Your Vendor Proof is now <strong style="color:#10b981;">active</strong>. You are now visible to procurement officers who filter by verified vendors on the BOOPPA platform.</p>
                <h3 style="color:#0f172a;">What changed on your profile</h3>
                <ul>
                  <li>Verification status: <strong>BASIC (Identity Verified, Active)</strong></li>
                  <li>Compliance score: <strong>{vp_score_display}</strong></li>
                  <li>Procurement readiness: <strong>{vp_readiness_label}</strong></li>
                  <li>CAL Level 1 activated — personalised upgrade recommendations will appear in your dashboard</li>
                </ul>
                <p style="color:#475569;font-size:13px;background:#f8fafc;border-left:3px solid #94a3b8;padding:10px 14px;border-radius:4px;">
                  <strong>What Vendor Proof attests:</strong> your identity and registration on BOOPPA — not a
                  compliance endorsement. Your procurement readiness above reflects your latest PDPA scan
                  {"(run a PDPA scan to establish it)" if _pdpa_compliance is None else ""}. Procurement officers
                  see your real standing on your verification page.
                </p>
                <h3 style="color:#0f172a;">Embed your Booppa Verified badge</h3>
                <p>Add this to your website or RFP proposals:</p>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-family:monospace;font-size:12px;word-break:break-all;">
                  {badge_html.replace('<', '&lt;').replace('>', '&gt;')}
                </div>
                <div style="margin-top:16px;">{badge_html}</div>
                {("<p style='margin-top:20px;color:#475569;font-size:13px;'>Your Vendor Proof certificate (valid until " + _vp_expires_display + ") is <strong>attached to this email as a PDF</strong>." + ("<br>You can also <a href='" + cert_url + "'>download it here</a>." if cert_url else "") + "</p>") if cert_pdf else ""}
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/vendor/dashboard" style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
                    Go to Dashboard →
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;margin-top:24px;">
                  Verification ID: {report_id}<br>
                  Verified on: {datetime.now(timezone.utc).strftime('%d %B %Y')}<br>
                  booppa.io
                </p>
              </div>
            </body></html>
            """
            try:
                email_svc = EmailService()
                # Deliver the certificate as a direct PDF attachment (not just an
                # expiring S3 link) so it is immediately fileable/forwardable.
                _attachments = None
                if cert_pdf:
                    _safe_co = (company_name or "certificate").replace("/", "-").replace(" ", "-")
                    _attachments = [(f"Vendor-Proof-Certificate-{_safe_co}.pdf", cert_pdf)]
                _ok = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Vendor Proof is Active — {company_name}",
                    body_html=body_html,
                    attachments=_attachments,
                )
                if not _ok:
                    logger.error("[VendorProof] delivery email rejected for %s", contact_email)
                    await _alert_payment_fulfillment_issue(
                        reason="Vendor Proof activation email rejected by provider",
                        product_type="vendor_proof",
                        customer_email=contact_email,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[VendorProof] Email failed for {contact_email}: {e}")

        logger.info(f"[VendorProof] Fulfilled {report_id} for vendor {vendor_id}")
        _maybe_fire_cover_sheet(contact_email)
    except Exception as e:
        logger.error(f"[VendorProof] Fulfillment error for {report_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def _fulfill_pdpa(report_id: str, customer_email: str | None, send_email: bool = True, raise_if_incomplete: bool = False) -> None:
    """
    PDPA Snapshot fulfillment:
    1. Run the full on-page + AI scan (if not already done)
    2. Generate branded PDF report
    3. Upload to S3
    4. Update vendor compliance score (+8 to +25 pts)
    5. Write CertificateLog entry
    6. Send email with PDF download link

    `send_email=False` suppresses the standalone "Snapshot Ready" email. The
    PDPA Monitor / Vendor Pro cycle uses this so the buyer gets a SINGLE
    consolidated PDPA deliverable email (the month-over-month Monitor report)
    instead of a raw Quick-Scan email plus the Monitor report — see
    `pdpa_monitor_monthly_rescan_task`. The score/PDF/CertificateLog side
    effects still run; only the email is gated.
    """
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[PDPA] Report {report_id} not found")
            return

        assessment = (
            report.assessment_data if isinstance(report.assessment_data, dict) else {}
        )
        contact_email = (
            customer_email
            or assessment.get("contact_email")
            or assessment.get("customer_email")
        )
        company_name = report.company_name or "Customer"
        website_url = report.company_website or assessment.get("website", "")

        # ── Step 1: Ensure scan is complete ────────────────────────────────
        # If the report already has a risk_score from a prior scan, use it.
        # Otherwise trigger the generic processing task synchronously.
        risk_score = assessment.get("risk_score") or (
            assessment.get("risk_assessment", {}).get("score")
            if isinstance(assessment.get("risk_assessment"), dict)
            else None
        )
        if risk_score is None:
            # Scan not yet run — queue generic processing; it will generate PDF too
            try:
                from app.workers.tasks import process_report_task

                process_report_task.delay(str(report.id))
                logger.info(
                    f"[PDPA] Queued generic scan for {report_id} (risk_score missing)"
                )
            except Exception as e:
                logger.error(f"[PDPA] Could not queue scan for {report_id}: {e}")
            
            if raise_if_incomplete:
                # Raise exception so the fulfill_pdpa_task retries and eventually writes
                # the Compliance score and CertificateLog when scan completes
                raise Exception("PDPA scan not yet complete, retrying later")
            return

        # ── Step 2: Generate PDF ────────────────────────────────────────────
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            pdf_data = {
                "report_id": report_id,
                "framework": report.framework or "pdpa_quick_scan",
                "company_name": company_name,
                "company_url": website_url,
                "created_at": (
                    report.created_at.isoformat()
                    if report.created_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                "status": "completed",
                "risk_score": risk_score,
                "risk_level": assessment.get("risk_level")
                or assessment.get("risk_assessment", {}).get("level", "MEDIUM"),
                "findings": assessment.get("findings")
                or assessment.get("detailed_findings", []),
                "summary": assessment.get("executive_summary", ""),
                # Pass structured report sections so PDF renders full findings + recommendations
                "executive_summary": assessment.get("executive_summary", ""),
                "detailed_findings": assessment.get("detailed_findings")
                or assessment.get("findings", []),
                "recommendations": assessment.get("recommendations", []),
                "legal_references": assessment.get("legal_references", []),
                "risk_assessment": assessment.get("risk_assessment", {}),
                # Screenshot — prefer stored base64, fallback to live capture
                "site_screenshot": assessment.get("site_screenshot")
                or assessment.get("screenshot"),
                "payment_confirmed": True,
                "tier": assessment.get("tier", "pro"),
                "contact_email": contact_email,
                "base_url": "https://www.booppa.io",
            }
            # Pass raw scan evidence so the dimension-weighted score this PDF
            # computes matches the canonical scan report (process_report_task).
            # Without these, _compliance_score_table scores from findings alone
            # and can diverge from the rich report — the 53-vs-54 bug.
            for _scan_key in (
                "security_headers", "consent_mechanism", "privacy_policy",
                "dpo_compliance", "dnc_mention", "nric_evidence", "nric",
                "policy_clauses", "pdpc_enforcement", "hosting", "trackers",
                "ssl_grade", "primary_language",
            ):
                if _scan_key in assessment:
                    pdf_data[_scan_key] = assessment[_scan_key]
            # Capture screenshot live if not already stored
            if not pdf_data["site_screenshot"] and website_url:
                try:
                    from app.services.screenshot_service import (
                        capture_screenshot_base64,
                    )

                    ss = capture_screenshot_base64(website_url)
                    if ss:
                        pdf_data["site_screenshot"] = ss
                        assessment["site_screenshot"] = ss
                        flag_modified(report, "assessment_data")
                        db.commit()
                except Exception as ss_err:
                    logger.warning(
                        f"[PDPA] Screenshot capture failed for {report_id}: {ss_err}"
                    )

            pdf_bytes = pdf_service.generate_pdf(pdf_data)
        except Exception as e:
            logger.error(f"[PDPA] PDF generation failed for {report_id}: {e}")

        # ── Step 3: Upload PDF to S3 ────────────────────────────────────────
        pdf_url = None
        if pdf_bytes:
            try:
                storage = S3Service()
                pdf_url = await storage.upload_pdf(pdf_bytes, report_id)
                report.s3_url = pdf_url
            except Exception as e:
                logger.error(f"[PDPA] S3 upload failed for {report_id}: {e}")

        # Mark report completed
        report.status = "completed"
        report.completed_at = datetime.now(timezone.utc)
        assessment["pdf_generated"] = True
        assessment["pdf_url"] = pdf_url
        assessment["on_page_only"] = False
        # Persist the EXACT headline compliance score this PDF printed (the
        # dimension-weighted overall, stashed by _compliance_score_table) and
        # the exact URL it displayed, so the Compliance Evidence Cover Sheet
        # reproduces both verbatim instead of recomputing and drifting (the
        # 53-vs-54 / crayon.com-vs-crayon.com/sg inconsistency in the audit).
        _computed_score = pdf_data.get("computed_overall_compliance_score")
        if _computed_score is None and isinstance(pdf_data.get("scan_data"), dict):
            # Defensive: _compliance_score_table stashes onto report_data["scan_data"]
            # when that nested key is present, so read it back from there too.
            _computed_score = pdf_data["scan_data"].get("computed_overall_compliance_score")
        if _computed_score is not None:
            assessment["compliance_score"] = _computed_score
        if website_url:
            assessment["display_url"] = website_url
        report.assessment_data = assessment
        from sqlalchemy.orm.attributes import flag_modified as _flag

        _flag(report, "assessment_data")
        db.commit()

        # ── Step 4: Update vendor compliance score (+8 to +25 pts) ─────────
        vendor_id = str(report.owner_id)
        try:
            from app.services.scoring import VendorScoreEngine

            VendorScoreEngine.update_vendor_score(db, vendor_id)
            logger.info(f"[PDPA] Vendor score updated for vendor {vendor_id}")
        except Exception as e:
            logger.error(f"[PDPA] Score update failed for vendor {vendor_id}: {e}")

        # ── Step 5: Write CertificateLog ────────────────────────────────────
        try:
            from app.core.models_v10 import CertificateLog

            cert = CertificateLog(
                vendor_id=report.owner_id,
                certificate_type="PDPA",
                report_id=report.id,
                file_key=report.file_key,
                generated_at=datetime.now(timezone.utc),
            )
            db.add(cert)
            db.commit()
        except Exception as e:
            logger.error(f"[PDPA] CertificateLog failed for {report_id}: {e}")

        # ── Step 6: Email PDF to vendor ────────────────────────────────────
        # Gated on send_email so the PDPA Monitor / Vendor Pro cycle can deliver
        # ONE consolidated PDPA email (the Monitor report) instead of two.
        if contact_email and send_email:
            try:
                download_section = (
                    f'<p style="margin-top:24px;">'
                    f'<a href="{pdf_url}" style="background-color:#10b981;color:#fff;'
                    f"padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;"
                    f'display:inline-block;">Download PDPA Snapshot Report (PDF)</a></p>'
                    if pdf_url
                    else "<p>Your report will be available on the BOOPPA dashboard shortly.</p>"
                )
                # Show the SAME compliance score the PDF + Cover Sheet show
                # (dimension-weighted, persisted), not a separate 100-risk figure
                # that drifted (e.g. 54 in the email vs 53 in the PDF).
                _email_compliance = assessment.get("compliance_score")
                if not isinstance(_email_compliance, (int, float)):
                    _email_compliance = 100 - int(risk_score or 50)
                _email_compliance = int(_email_compliance)
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:20px;">Your PDPA Snapshot is Ready</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>Hello <strong>{company_name}</strong>,</p>
                    <p>Your PDPA Snapshot report for <strong>{website_url}</strong> has been generated.</p>
                    <p>The report evaluates your compliance across 8 PDPA dimensions — consent, data flow,
                       DSAR procedures, breach notification, retention, third-party processors, DPO, and
                       privacy notice — and provides specific recommendations with legislative references.</p>
                    <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;
                                border-radius:4px;margin:20px 0;">
                      <strong>Compliance Score:</strong> {_email_compliance}/100<br>
                      <strong>Report ID:</strong> {report_id[:8].upper()}<br>
                      <strong>Generated:</strong> {datetime.now(timezone.utc).strftime('%d %B %Y')}
                    </div>
                    <p>Your compliance score on BOOPPA has been updated to reflect this scan.
                       Procurement officers searching for verified vendors will see your improved standing.</p>
                    {download_section}
                    <p style="margin-top:24px;">
                      <a href="https://www.booppa.io/vendor/dashboard"
                         style="color:#10b981;text-decoration:underline;">View your dashboard →</a>
                    </p>
                    <p style="color:#64748b;font-size:11px;margin-top:24px;">
                      This report is for informational purposes only and does not constitute legal advice
                      or PDPC certification. BOOPPA is not a law firm.
                    </p>
                  </div>
                </body></html>
                """
                email_svc = EmailService()
                _attachments = None
                if pdf_bytes:
                    _safe_co = (company_name or "report").replace("/", "-").replace(" ", "-")
                    _attachments = [(f"PDPA_Snapshot_{_safe_co}.pdf", pdf_bytes)]
                sent = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your PDPA Snapshot Report is Ready — {company_name}",
                    body_html=body_html,
                    attachments=_attachments,
                )
                if not sent:
                    await _alert_payment_fulfillment_issue(
                        reason="PDPA snapshot report email rejected by provider",
                        product_type="pdpa_quick_scan",
                        customer_email=contact_email,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[PDPA] Email failed for {contact_email}: {e}")

        logger.info(
            f"[PDPA] Fulfilled {report_id} for vendor {vendor_id} pdf={pdf_url}"
        )
        _maybe_fire_cover_sheet(contact_email)
    except Exception as e:
        logger.error(f"[PDPA] Fulfillment error for {report_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def _fire_strategy_6(sector: str | None, buyer_rfp_title: str) -> None:
    """
    Strategy 6: Notify the top 5 verified vendors in the same sector
    that they have been shortlisted for a new procurement opportunity.
    Buyer identity is never disclosed.
    Only fires for rfp_express (not rfp_complete).
    """
    if not sector:
        logger.info("[Strategy6] No sector — skipping")
        return

    db = SessionLocal()
    try:
        from app.core.models_v6 import (
            VerifyRecord,
            LifecycleStatus,
            VendorSector,
            VendorScore,
        )
        from app.core.models import User

        # Get top 5 active verified vendors in sector, ordered by compliance score desc
        verified_vendor_ids = (
            db.query(VerifyRecord.vendor_id)
            .filter(VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE)
            .subquery()
        )
        sector_vendor_ids = (
            db.query(VendorSector.vendor_id)
            .filter(VendorSector.sector.ilike(f"%{sector}%"))
            .subquery()
        )
        top_vendors = (
            db.query(User, VendorScore)
            .join(VendorScore, VendorScore.vendor_id == User.id)
            .filter(User.id.in_(db.query(verified_vendor_ids)))
            .filter(User.id.in_(db.query(sector_vendor_ids)))
            .order_by(VendorScore.total_score.desc())
            .limit(5)
            .all()
        )

        if not top_vendors:
            logger.info(f"[Strategy6] No verified vendors found in sector '{sector}'")
            return

        email_svc = EmailService()
        for user, score in top_vendors:
            if not user.email:
                continue
            try:
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:18px;">You Were Shortlisted</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>A procurement team is actively evaluating vendors in the <strong>{sector}</strong> sector for a new opportunity.</p>
                    <p>Your verified status on BOOPPA placed you in the <strong>top 5 shortlisted vendors</strong> for this opportunity.</p>
                    <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;border-radius:4px;margin:20px 0;">
                      <strong>Opportunity:</strong> {buyer_rfp_title or 'New procurement in your sector'}<br>
                      <strong>Your sector:</strong> {sector}<br>
                      <strong>Buyer:</strong> Identity confidential — standard procurement practice
                    </div>
                    <p>To improve your position in future shortlists, strengthen your evidence package:</p>
                    <p>
                      <a href="https://www.booppa.io/vendor/dashboard" style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
                        View Dashboard →
                      </a>
                    </p>
                    <p style="color:#64748b;font-size:11px;margin-top:24px;">
                      You are receiving this because your vendor profile is verified on BOOPPA.<br>
                      Buyer details are kept confidential per procurement best practice.
                    </p>
                  </div>
                </body></html>
                """
                await email_svc.send_html_email(
                    to_email=user.email,
                    subject="You Were Shortlisted — New Procurement Opportunity",
                    body_html=body_html,
                )
                logger.info(
                    f"[Strategy6] Notified vendor {user.email} for sector {sector}"
                )
            except Exception as e:
                logger.warning(f"[Strategy6] Email failed for {user.email}: {e}")

    except Exception as e:
        logger.error(f"[Strategy6] Failed: {e}")
    finally:
        db.close()


def _rollback_webhook_idempotency(event_id: str | None) -> None:
    """
    Delete the ProcessedWebhookEvent row so a Stripe retry can re-process.
    Called when the handler raises uncaught — otherwise Stripe's retry would
    see "already_processed" and skip fulfillment (user paid, never received).
    """
    if not event_id:
        return
    try:
        from app.core.models import ProcessedWebhookEvent

        _db = SessionLocal()
        try:
            _db.query(ProcessedWebhookEvent).filter(
                ProcessedWebhookEvent.event_id == event_id
            ).delete()
            _db.commit()
        finally:
            _db.close()
        logger.warning(
            f"[Webhook] Rolled back idempotency row for {event_id} after handler failure"
        )
    except Exception as e:
        logger.error(f"[Webhook] Idempotency rollback failed for {event_id}: {e}")


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Thin wrapper around the actual webhook handler. Owns idempotency rollback
    so an uncaught handler exception doesn't permanently mark the event as
    processed (which would short-circuit Stripe's retry).
    """
    event_id_holder: dict[str, str | None] = {"event_id": None}
    try:
        return await _stripe_webhook_impl(request, event_id_holder)
    except HTTPException:
        # Signature failures, etc. — don't roll back; they're already terminal.
        raise
    except Exception as exc:
        _rollback_webhook_idempotency(event_id_holder.get("event_id"))
        logger.exception(
            f"[Webhook] Unhandled handler error for {event_id_holder.get('event_id')}: {exc}"
        )
        # Returning 500 lets Stripe retry; rollback above ensures the retry isn't skipped.
        raise HTTPException(status_code=500, detail="Webhook processing failed")


async def _stripe_webhook_impl(
    request: Request, event_id_holder: dict[str, str | None]
):
    """Handle Stripe webhooks. Verifies signature and processes checkout.session.completed events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    webhook_secret = settings.STRIPE_WEBHOOK_SECRET
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET not configured")
        raise HTTPException(status_code=500, detail="Webhook not configured")

    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=sig_header, secret=webhook_secret
        )
    except Exception as e:
        logger.error(f"Stripe webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Idempotency guard: atomic INSERT ON CONFLICT to prevent race conditions
    event_id = event["id"]
    # Publish to the wrapper so it can roll the row back on handler failure.
    event_id_holder["event_id"] = event_id
    if event_id:
        try:
            from app.core.models import ProcessedWebhookEvent
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            _idem_db = SessionLocal()
            try:
                stmt = (
                    pg_insert(ProcessedWebhookEvent)
                    .values(event_id=event_id, event_type=event["type"])
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
                result = _idem_db.execute(stmt)
                _idem_db.commit()
                if result.rowcount == 0:
                    logger.info(f"[Webhook] Duplicate event {event_id} — skipping")
                    return {"status": "already_processed"}
            finally:
                _idem_db.close()
        except Exception as e:
            logger.warning(f"[Webhook] Idempotency check failed (non-fatal): {e}")

    # Handle the checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        # Parse session from raw payload (plain dict) — avoids StripeObject .get() issues
        raw = json.loads(payload)
        session = raw.get("data", {}).get("object", {})
        metadata = session.get("metadata") or {}

        # Try multiple metadata keys for report id
        report_id = (
            metadata.get("report_id")
            or metadata.get("reportId")
            or session.get("client_reference_id")
        )
        product_type = metadata.get("product_type")
        customer_email = (
            (session.get("customer_details") or {}).get("email")
            or session.get("customer_email")
            or metadata.get("customer_email")
        )

        # Record PAYMENT funnel event (non-blocking)
        try:
            from app.services.funnel_analytics import record_funnel_event

            _fdb = SessionLocal()
            record_funnel_event(
                _fdb,
                stage="PAYMENT",
                session_id=session.get("id"),
                source="stripe",
                metadata={"product_type": product_type, "email": customer_email},
            )
            _fdb.commit()
            _fdb.close()
        except Exception:
            pass

        if not report_id:
            # Subscriptions have no report — activate directly (synchronous)
            if product_type in SUBSCRIPTION_PRODUCT_TYPES:
                stripe_sub_id = session.get("subscription")
                stripe_cust_id = session.get("customer")
                # Demo fire-all fires ONLY on an explicit test-mode event
                # (Stripe always stamps `livemode`; we require it to be exactly
                # False so a missing/true value can never trigger the demo path
                # for a real live buyer).
                demo_checkout = raw.get("livemode") is False
                # Activate synchronously so plan is set and email sent
                # immediately — does not depend on Celery workers being up.
                await _activate_subscription(
                    product_type=product_type,
                    customer_email=customer_email,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_customer_id=stripe_cust_id,
                    demo=demo_checkout,
                )
                logger.info(
                    f"Activated subscription for {product_type} email={customer_email}"
                )
                return {"received": True}

            # Bundles are self-contained — fan out to component fulfillment tasks
            if product_type in BUNDLE_COMPONENTS:
                from app.workers.tasks import fulfill_bundle_task

                fulfill_bundle_task.delay(
                    product_type=product_type,
                    report_id=None,
                    customer_email=customer_email,
                    metadata=metadata,
                    session_id=session.get("id"),
                )
                logger.info(
                    f"Queued bundle fulfillment for {product_type} email={customer_email}"
                )
                return {"received": True}

            # RFP products are self-contained — no pre-existing Report record required
            if product_type in RFP_PRODUCT_TYPES:
                # Workflow rule: every RFP purchase MUST go through the brief
                # intake before the kit is generated. We never queue
                # fulfill_rfp_task at webhook time anymore, even when checkout
                # collected an rfp_description — the buyer re-confirms the
                # facts on /rfp-intake/{id} so they own the inputs we anchor.
                vendor_url = metadata.get("vendor_url", "")
                company_name = metadata.get("company_name", "")
                await _defer_rfp_to_intake(
                    rfp_product_type=product_type,
                    bundle_source=product_type,
                    customer_email=customer_email,
                    vendor_url=vendor_url or None,
                    company_name=company_name or None,
                    session_id=session.get("id"),
                )
                return {"received": True}

            # Standalone /pricing purchases that don't carry a pre-existing report_id:
            #   - pdpa_quick_scan / pdpa_snapshot → create stub Report + queue PDPA task
            #   - vendor_proof → create stub Report + queue Vendor Proof task
            #   - compliance_notarization_{1,10,50} → grant credits + email redemption link
            if await _fulfill_standalone_no_report(
                product_type=product_type,
                customer_email=customer_email,
                metadata=metadata,
                session_id=session.get("id"),
            ):
                return {"received": True}

            await _alert_payment_fulfillment_issue(
                reason="checkout session completed but no handler matched (no report_id and unknown product_type)",
                product_type=product_type,
                customer_email=customer_email,
                session_id=session.get("id"),
                event_id=event_id,
                extra={"metadata_keys": sorted(metadata.keys())},
            )
            return {"received": True}

        db = SessionLocal()
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if not report:
                logger.error(
                    f"Report {report_id} not found for Stripe session {session.get('id', '?')}"
                )
                return {"received": True}

            # Mark payment confirmed in assessment_data
            try:
                ad = report.assessment_data or {}
                if not isinstance(ad, dict):
                    ad = json.loads(ad)
            except Exception:
                ad = {}

            ad["payment_confirmed"] = True
            if product_type:
                ad["product_type"] = product_type
            if customer_email:
                ad["contact_email"] = customer_email

            policy = enforce_tier(ad, report.framework)
            ad["tier"] = policy.get("tier")
            ad["tier_features"] = policy.get("features")

            # Ensure PDF generation is attempted for paid tiers
            if policy.get("features", {}).get("pdf"):
                ad["on_page_only"] = False

            report.assessment_data = ad
            flag_modified(report, "assessment_data")
            db.commit()

            # Vendor Proof — create VerifyRecord + VendorStatusSnapshot + send badge email
            if product_type in VENDOR_PROOF_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_vendor_proof_task

                fulfill_vendor_proof_task.delay(str(report.id), customer_email)
                logger.info(f"Queued vendor_proof fulfillment for report {report_id}")

            # PDPA Snapshot — scan, PDF, score update, CertificateLog, email
            elif product_type in PDPA_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_pdpa_task

                fulfill_pdpa_task.delay(str(report.id), customer_email)
                logger.info(f"Queued pdpa fulfillment for report {report_id}")

            # Notarization — lightweight anchor + certificate (no AI, no website scan)
            elif product_type in NOTARIZATION_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_notarization_task

                fulfill_notarization_task.delay(str(report.id), customer_email)
                logger.info(f"Queued notarization fulfillment for report {report_id}")

            # RFP Express / Complete — generate PDF + email immediately
            elif product_type in RFP_PRODUCT_TYPES:
                vendor_id = metadata.get("vendor_id") or str(report.owner_id)
                vendor_url = metadata.get("vendor_url") or metadata.get(
                    "website_url", ""
                )
                company_name = metadata.get("company_name") or metadata.get(
                    "company", ""
                )
                rfp_desc = (metadata.get("rfp_description") or "").strip()
                # No brief on file → defer to /rfp-intake instead of generating a placeholder kit.
                if not rfp_desc:
                    await _defer_rfp_to_intake(
                        rfp_product_type=product_type,
                        bundle_source=product_type,
                        customer_email=customer_email,
                        vendor_url=vendor_url or None,
                        company_name=company_name or None,
                        session_id=session.get("id"),
                    )
                elif not vendor_url or not company_name:
                    await _alert_payment_fulfillment_issue(
                        reason="RFP fulfillment skipped: missing vendor_url or company_name (with report_id, had description)",
                        product_type=product_type,
                        customer_email=customer_email,
                        session_id=session.get("id"),
                        event_id=event_id,
                        extra={
                            "report_id": str(report_id),
                            "metadata_keys": sorted(metadata.keys()),
                        },
                    )
                else:
                    from app.workers.tasks import fulfill_rfp_task

                    session_id = session.get("id")
                    intake_dict = None
                    if metadata.get("has_intake") == "1" and session_id:
                        from app.core.cache import cache as cache_mod

                        cached_intake = cache_mod.get(
                            cache_mod.cache_key(f"rfp_intake:{session_id}")
                        )
                        if isinstance(cached_intake, dict):
                            intake_dict = cached_intake
                    fulfill_rfp_task.delay(
                        product_type=product_type,
                        vendor_id=vendor_id,
                        vendor_email=customer_email or "",
                        vendor_url=vendor_url,
                        company_name=company_name,
                        rfp_description=rfp_desc,
                        session_id=session.get("id"),
                        intake_data=intake_dict,
                    )
                    logger.info(
                        f"Queued RFP {product_type} fulfillment for vendor {vendor_id}"
                    )

                    # Strategy 6: notify top-5 verified sector peers (rfp_express only)
                    if product_type == "rfp_express":
                        sector = metadata.get("sector") or (intake_dict or {}).get(
                            "sector"
                        )
                        rfp_title = (
                            rfp_desc
                            or metadata.get("rfp_description")
                            or "New procurement opportunity"
                        )
                        from app.workers.tasks import fire_strategy_6_task

                        fire_strategy_6_task.delay(sector, rfp_title)

            else:
                # Standard report: trigger async processing via Celery
                try:
                    from app.workers.tasks import process_report_task

                    process_report_task.delay(str(report.id))
                    logger.info(
                        f"Queued background processing for paid report {report_id}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to queue background task for {report_id}: {e}"
                    )

        finally:
            db.close()

    # ── Post-checkout: upgrade user plan + close referral reward loop ────────
    if event["type"] == "checkout.session.completed":
        raw = json.loads(payload) if isinstance(payload, (str, bytes)) else {}
        session = raw.get("data", {}).get("object", {}) if raw else {}
        _meta2 = session.get("metadata") or {}
        customer_email = (
            (session.get("customer_details") or {}).get("email")
            or session.get("customer_email")
            or _meta2.get("customer_email")
        )
        if customer_email:
            _db = SessionLocal()
            try:
                user = _db.query(User).filter(User.email == customer_email).first()
                if user:
                    metadata = session.get("metadata") or {}
                    product = metadata.get("product_type") or ""
                    # Subscriptions are already handled by the first
                    # checkout.session.completed block via _activate_subscription.
                    if product in SUBSCRIPTION_PRODUCT_TYPES:
                        # Already activated by the first checkout.session.completed block;
                        # only handle referral reward here.
                        # Row-locked: prevents two concurrent checkouts for the same
                        # referred user from both marking the same Referral REWARDED
                        # and double-paying the reward.
                        referral = (
                            _db.query(Referral)
                            .filter(
                                Referral.referred_id == user.id,
                                Referral.status == "SIGNED_UP",
                                Referral.reward_claimed == False,
                            )
                            .with_for_update()
                            .first()
                        )
                        if referral:
                            referral.status = "REWARDED"
                            referral.reward_claimed = True
                            referral.reward_claimed_at = datetime.now(timezone.utc)
                            referral.reward_type = "30_DAYS_FREE"
                        _db.commit()
                    else:
                        _checkout_plan_map = {
                            "enterprise_monthly": "enterprise",
                            "enterprise_pro_monthly": "enterprise_pro",
                        }
                        new_plan = _checkout_plan_map.get(product)
                        if not new_plan:
                            new_plan = (
                                "enterprise" if "enterprise" in product else "pro"
                            )
                        user.plan = new_plan
                        user.subscription_tier = new_plan
                        try:
                            from datetime import datetime, timezone as _tz

                            user.subscription_started_at = datetime.now(_tz.utc)
                        except Exception:
                            pass
                        # Close the referral reward loop.
                        # Row-locked to prevent concurrent claims (see comment above).
                        referral = (
                            _db.query(Referral)
                            .filter(
                                Referral.referred_id == user.id,
                                Referral.status == "SIGNED_UP",
                                Referral.reward_claimed == False,
                            )
                            .with_for_update()
                            .first()
                        )
                        if referral:
                            referral.status = "REWARDED"
                            referral.reward_claimed = True
                            referral.reward_claimed_at = datetime.now(timezone.utc)
                            referral.reward_type = "30_DAYS_FREE"
                            _db.commit()
                            try:
                                referrer = (
                                    _db.query(User)
                                    .filter(User.id == referral.referrer_id)
                                    .first()
                                )
                                if referrer and referrer.email:
                                    from app.workers.tasks import (
                                        send_referral_reward_email_task,
                                    )

                                    send_referral_reward_email_task.delay(
                                        referrer.email
                                    )
                            except Exception as ref_email_exc:
                                logger.warning(
                                    f"[Referral] Referrer email failed: {ref_email_exc}"
                                )
                        else:
                            _db.commit()
                        logger.info(
                            f"[Webhook] Upgraded user {customer_email} to plan={new_plan}"
                            + (
                                f"; referral {referral.referral_code} rewarded"
                                if referral
                                else ""
                            )
                        )
                        # Queue post-payment D+1 drip (24h delay per brief)
                        try:
                            from app.workers.tasks import post_payment_drip

                            post_payment_drip.apply_async(
                                kwargs={
                                    "vendor_email": customer_email,
                                    "product_type": product,
                                    "company_name": metadata.get("company_name", ""),
                                    "report_id": str(report_id) if report_id else "",
                                },
                                countdown=86400,  # 24 hours
                            )
                        except Exception as drip_exc:
                            logger.warning(
                                f"[Webhook] post_payment_drip queue failed: {drip_exc}"
                            )
            except Exception as exc:
                logger.error(
                    f"[Webhook] Plan upgrade failed for {customer_email}: {exc}"
                )
            finally:
                _db.close()

    # ── Subscription lifecycle events ────────────────────────────────────────
    # Stripe fires `customer.subscription.updated` with status=canceled when a
    # cancel happens, and `customer.subscription.deleted` when the row is
    # finally removed (after the cancel period ends). We handle both so the
    # downgrade flow doesn't depend on which one Stripe sends first.
    if event["type"] in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        # `.deleted` events always represent a finalised cancel — coerce the
        # sub_status into "canceled" so the cancel branch below fires.
        if event["type"] == "customer.subscription.deleted":
            _force_canceled = True
        else:
            _force_canceled = False
        raw = json.loads(payload)
        sub = raw.get("data", {}).get("object", {})
        stripe_sub_id = sub.get("id")
        stripe_cust_id = sub.get("customer")
        sub_status = sub.get("status")
        cust_email = None
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            cust = stripe.Customer.retrieve(stripe_cust_id) if stripe_cust_id else None
            cust_email = (
                (cust or {}).get("email")
                if isinstance(cust, dict)
                else getattr(cust, "email", None)
            )
        except Exception:
            pass

        # `.deleted` always means canceled, regardless of what status the
        # payload claims (Stripe sometimes leaves the last-seen status on the
        # row when it deletes).
        if _force_canceled:
            sub_status = "canceled"

        if stripe_sub_id and sub_status in ("active", "trialing"):
            items = sub.get("items", {}).get("data", [])
            product_type_sub = None
            import os as _os

            for item in items:
                price_id = (item.get("price") or {}).get("id") or (
                    item.get("plan") or {}
                ).get("id")
                if price_id:
                    for key in (
                        "vendor_active_monthly",
                        "vendor_active_annual",
                        "pdpa_monitor_monthly",
                        "pdpa_monitor_annual",
                        "enterprise_monthly",
                        "enterprise_pro_monthly",
                    ):
                        env_price = _os.environ.get(
                            f"STRIPE_{key.upper()}"
                        ) or _os.environ.get(f"NEXT_PUBLIC_STRIPE_{key.upper()}")
                        if env_price and env_price == price_id:
                            product_type_sub = key
                            break
            if product_type_sub and cust_email:
                from app.workers.tasks import activate_subscription_task

                activate_subscription_task.delay(
                    product_type=product_type_sub,
                    customer_email=cust_email,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_customer_id=stripe_cust_id,
                )
                logger.info(
                    f"[Webhook] Subscription {event['type']} → plan={product_type_sub} email={cust_email}"
                )

                # Upsert local Subscription record
                try:
                    from app.core.db import SessionLocal as _SL
                    from app.core.models import Subscription as _Sub, User as _User
                    from datetime import datetime, timezone as _tz

                    _sdb = _SL()
                    try:
                        row = (
                            _sdb.query(_Sub)
                            .filter(_Sub.stripe_subscription_id == stripe_sub_id)
                            .first()
                        )
                        period_end_ts = sub.get("current_period_end") or sub.get(
                            "current_period_start"
                        )
                        period_end = None
                        if period_end_ts:
                            try:
                                period_end = datetime.fromtimestamp(
                                    int(period_end_ts), _tz.utc
                                )
                            except Exception:
                                period_end = None

                        if row:
                            row.status = sub_status
                            row.stripe_customer_id = stripe_cust_id
                            row.current_period_end = period_end
                            row.metadata_json = sub.get("metadata") or {}
                        else:
                            uid = None
                            if cust_email:
                                u = (
                                    _sdb.query(_User)
                                    .filter(_User.email == cust_email)
                                    .first()
                                )
                                if u:
                                    uid = u.id
                            new = _Sub(
                                user_id=uid,
                                stripe_subscription_id=stripe_sub_id,
                                stripe_customer_id=stripe_cust_id,
                                product_type=product_type_sub,
                                status=sub_status,
                                current_period_end=period_end,
                                metadata=sub.get("metadata") or {},
                            )
                            _sdb.add(new)
                        _sdb.commit()
                    finally:
                        _sdb.close()
                except Exception as _e:
                    logger.warning(f"[Webhook] Subscription upsert failed: {_e}")

        elif sub_status == "canceled":
            _db3 = SessionLocal()
            try:
                if cust_email:
                    user = _db3.query(User).filter(User.email == cust_email).first()
                    if user:
                        # Mark the specific Subscription row as canceled
                        from app.core.models import Subscription as SubModel

                        if stripe_sub_id:
                            sub_row = (
                                _db3.query(SubModel)
                                .filter(
                                    SubModel.stripe_subscription_id == stripe_sub_id
                                )
                                .first()
                            )
                            if sub_row:
                                sub_row.status = "canceled"
                                _db3.flush()

                        # Derive user.plan from remaining active subscriptions
                        _plan_map = {
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
                            # Buyer ladder — monthly + annual share a plan family.
                            "buyer_starter_monthly": "buyer_starter",
                            "buyer_starter_annual": "buyer_starter",
                            "buyer_pro_monthly": "buyer_pro",
                            "buyer_pro_annual": "buyer_pro",
                            "buyer_enterprise_monthly": "buyer_enterprise",
                            "buyer_enterprise_annual": "buyer_enterprise",
                        }
                        remaining = (
                            _db3.query(SubModel)
                            .filter(
                                SubModel.user_id == user.id,
                                SubModel.status.in_(("active", "trialing")),
                            )
                            .all()
                        )
                        if remaining:
                            # Set plan to the last remaining active subscription
                            user.plan = _plan_map.get(remaining[-1].product_type, "pro")
                        else:
                            user.plan = "free"
                        user.subscription_tier = user.plan
                        if hasattr(user, "stripe_subscription_id"):
                            user.stripe_subscription_id = None
                        _db3.commit()
                        logger.info(
                            f"[Webhook] Subscription canceled for {cust_email}, plan now={user.plan}"
                        )
                        try:
                            _revert_subscription_score_lever(
                                _db3,
                                user.id,
                                None if user.plan == "free" else user.plan,
                            )
                        except Exception as lever_err:
                            logger.warning(
                                f"[Webhook] Score lever revert failed for {cust_email}: {lever_err}"
                            )

                        # Refresh seat caps on every org this user owns so a
                        # downgrade (e.g. Pro -> Starter -> free) shrinks the
                        # cap and blocks further invites. Existing members are
                        # NOT evicted — only the next invite is gated.
                        try:
                            from app.billing.enforcement import max_seats_for
                            from app.core.models_enterprise import Organisation as _Org

                            new_cap = max_seats_for(user.plan)
                            owned_orgs = (
                                _db3.query(_Org)
                                .filter(
                                    _Org.owner_user_id == user.id,
                                )
                                .all()
                            )
                            for _org in owned_orgs:
                                _org.max_seats = new_cap
                            if owned_orgs:
                                _db3.commit()
                                logger.info(
                                    f"[Webhook] Refreshed max_seats={new_cap} on "
                                    f"{len(owned_orgs)} org(s) for {cust_email}"
                                )
                        except Exception as seat_err:
                            logger.warning(
                                f"[Webhook] Seat-cap refresh failed for {cust_email}: {seat_err}"
                            )

                        # If the user's new plan no longer includes Pro Suite,
                        # deactivate SAML SSO on each owned org so the login
                        # URLs stop minting Booppa tokens. White-label config
                        # and saved IdP metadata stay (re-activating is just
                        # a flag flip after the customer re-subscribes).
                        try:
                            from app.billing.enforcement import PRO_SUITE_PLAN_KEYS
                            from app.core.models_enterprise import (
                                Organisation as _Org2,
                                SsoConfig as _SsoCfg,
                            )

                            if user.plan not in PRO_SUITE_PLAN_KEYS:
                                org_ids = [
                                    o.id
                                    for o in _db3.query(_Org2)
                                    .filter(_Org2.owner_user_id == user.id)
                                    .all()
                                ]
                                if org_ids:
                                    deactivated = (
                                        _db3.query(_SsoCfg)
                                        .filter(
                                            _SsoCfg.organisation_id.in_(org_ids),
                                            _SsoCfg.is_active == True,  # noqa: E712
                                        )
                                        .update(
                                            {"is_active": False},
                                            synchronize_session=False,
                                        )
                                    )
                                    if deactivated:
                                        _db3.commit()
                                        logger.info(
                                            f"[Webhook] Deactivated SSO on {deactivated} org(s) "
                                            f"for {cust_email} (lapsed Pro Suite)"
                                        )
                        except Exception as sso_err:
                            logger.warning(
                                f"[Webhook] SSO deactivation failed for {cust_email}: {sso_err}"
                            )
            except Exception as exc:
                logger.error(f"[Webhook] Cancellation handling failed: {exc}")
            finally:
                _db3.close()

            # mark subscription row as canceled if exists
            try:
                from app.core.db import SessionLocal as _SL2
                from app.core.models import Subscription as _Sub2

                _d2 = _SL2()
                try:
                    r = (
                        _d2.query(_Sub2)
                        .filter(_Sub2.stripe_subscription_id == stripe_sub_id)
                        .first()
                    )
                    if r:
                        r.status = "canceled"
                        _d2.commit()
                finally:
                    _d2.close()
            except Exception:
                pass

    # ── Invoice renewal — trigger monthly health checks ───────────────────────
    if event["type"] == "invoice.payment_succeeded":
        raw = json.loads(payload)
        inv = raw.get("data", {}).get("object", {})
        if inv.get("billing_reason") in ("subscription_cycle", "subscription_update"):
            cust_email_inv = inv.get("customer_email")
            if cust_email_inv:
                _db4 = SessionLocal()
                try:
                    user = _db4.query(User).filter(User.email == cust_email_inv).first()
                    user_plan = getattr(user, "plan", "") if user else ""
                    if user and "vendor_active" in user_plan:
                        from app.workers.tasks import vendor_active_health_check_task

                        vendor_active_health_check_task.delay(
                            str(user.id), cust_email_inv
                        )
                        logger.info(
                            f"[Webhook] Queued monthly health check for {cust_email_inv}"
                        )
                    # PDPA Monitor's monthly deliverable is the month-over-month
                    # Monitor report (delta + drift + a folded-in regulatory
                    # briefing), fired by the `run_pdpa_monitor_monthly_rescans`
                    # beat task. We intentionally do NOT also send the standalone
                    # generic "regulatory alert" email here — that was a second,
                    # canned email per cycle (inbox spam the forensic audit flagged).
                    # See `run_pdpa_monitor_report_for_user`.
                except Exception as exc:
                    logger.error(f"[Webhook] Invoice renewal hook failed: {exc}")
                finally:
                    _db4.close()

    # Record ACTIVE funnel event after all fulfillment (non-blocking)
    try:
        from app.services.funnel_analytics import record_funnel_event

        _adb = SessionLocal()
        record_funnel_event(
            _adb,
            stage="ACTIVE",
            metadata={"event_type": event["type"]},
        )
        _adb.commit()
        _adb.close()
    except Exception:
        pass

    return {"received": True}
