from app.core.route_classes import RetryAPIRoute
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
from app.core.models import Referral
from datetime import datetime, timedelta, timezone
import stripe
import logging
import json
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RetryAPIRoute)


def _emit_subscription_webhook(event_type: str, customer_email: str, product_type: str, extra: dict) -> None:
    """Resolve the buyer by email and queue a subscription lifecycle webhook.

    Best-effort — a missing user, org, or endpoint is a silent no-op so
    fulfillment never breaks on webhook emission.
    """
    try:
        from app.workers.tasks import _emit_user_webhook
        _db = SessionLocal()
        try:
            user = _db.query(User).filter(User.email == customer_email).first()
            if not user:
                return
            _emit_user_webhook(_db, user.id, event_type, {
                "product_type": product_type,
                "plan": getattr(user, "plan", None),
                **(extra or {}),
            })
        finally:
            _db.close()
    except Exception as e:
        logger.warning("subscription webhook emit skipped (%s): %s", event_type, e)


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


from app.services.fulfillment import (
    activate_subscription as _activate_subscription,
    fulfill_standalone_no_report as _fulfill_standalone_no_report,
    fulfill_pdpa as _fulfill_pdpa,
    fulfill_vendor_proof as _fulfill_vendor_proof,
    fulfill_notarization as _fulfill_notarization,
    fulfill_rfp_package as _fulfill_rfp_package,
    fulfill_bundle as _fulfill_bundle,
    fulfill_compliance_evidence_pack as _fulfill_compliance_evidence_pack,
    defer_rfp_to_intake as _defer_rfp_to_intake,
    maybe_fire_cover_sheet as _maybe_fire_cover_sheet,
    fire_strategy_6 as _fire_strategy_6,
    alert_payment_fulfillment_issue as _alert_payment_fulfillment_issue,
    create_stub_report as _create_stub_report,
    revert_subscription_score_lever as _revert_subscription_score_lever,
)
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


# NOTE: idempotency is enforced by ProcessedWebhookEvent (atomic INSERT ... ON
# CONFLICT on the stable Stripe event_id inside _stripe_webhook_impl). The old
# IdempotencyGuard keyed on the Stripe-Signature header was both ineffective and
# harmful: Stripe re-signs every redelivery (the signature carries a timestamp),
# so it never deduped real retries, and it set an IN_PROGRESS marker it never
# cleared — a second delivery of the same signature 409'd for 24h.
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
                _emit_subscription_webhook(
                    "subscription.activated", customer_email, product_type,
                    {"stripe_subscription_id": stripe_sub_id},
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

            # Bundles — self-contained, fan out to component fulfillment (mirrors
            # the no-report_id path at line ~317). Without this guard a bundle that
            # carries a report_id matched none of the elif arms above, fell through
            # to process_report_task (never fulfilled as a bundle) AND then to the
            # plan-upgrade block below, granting enterprise/pro from a one-time buy.
            elif product_type in BUNDLE_COMPONENTS:
                from app.workers.tasks import fulfill_bundle_task

                fulfill_bundle_task.delay(
                    product_type=product_type,
                    report_id=str(report.id),
                    customer_email=customer_email,
                    metadata=metadata,
                    session_id=session.get("id"),
                )
                logger.info(
                    f"Queued bundle fulfillment for {product_type} (report {report_id})"
                )
                # Return early: bundles must not reach the plan-upgrade block.
                return {"received": True}

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
                        from app.core.repositories.referral_repository import ReferralRepository
                        referral = ReferralRepository.get_unclaimed_by_referred_id_for_update(_db, str(user.id))
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
                            user.subscription_started_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                        # Close the referral reward loop.
                        # Row-locked to prevent concurrent claims (see comment above).
                        from app.core.repositories.referral_repository import ReferralRepository
                        referral = ReferralRepository.get_unclaimed_by_referred_id_for_update(_db, str(user.id))
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
                    # Resolve against EVERY subscription SKU, not a hardcoded 6-key
                    # subset. A portal-driven plan change (or a delayed/retried
                    # subscription event) arrives only through this event; the old
                    # short list left newer tiers (buyer_*, *_suite, tender_*,
                    # vendor_pro_*, compliance_notarization_10/50, csp_*) unresolved,
                    # so the upgrade never activated and user.plan went stale.
                    for key in sorted(SUBSCRIPTION_PRODUCT_TYPES):
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
                                    int(period_end_ts), timezone.utc
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
                            from app.workers.tasks import _emit_user_webhook
                            _emit_user_webhook(_db3, user.id, "subscription.canceled", {
                                "stripe_subscription_id": stripe_sub_id,
                                "plan": user.plan,
                            })
                        except Exception as _wh_err:
                            logger.warning(f"[Webhook] canceled emit skipped: {_wh_err}")
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
                            from app.core.models import Organisation as _Org

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
                            from app.core.models import (
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
