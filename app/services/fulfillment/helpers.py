from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service


from app.services.email_service import EmailService
from app.services.email_layout import (
    branded_email_html,
    email_button,
    email_info_box,
)
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


def _log_purchase_activity(
    db, vendor_id, activity_type: str, description: str, extra: dict | None = None
) -> None:
    """Record a row in ActivityLog so Engagement + Recency score components
    reflect paid actions (purchases, renewals, fulfillments)."""
    if not vendor_id:
        return
    try:
        from app.core.models import ActivityLog

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
        from app.core.models import VerifyRecord as _VR, VerificationLevel as _VL

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
        from app.core.models import VerifyRecord as _VR, VerificationLevel as _VL
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

    # Idempotency: a failing fulfillment (e.g. out-of-gas anchoring) is retried by
    # Celery, and each retry re-enters this alert. Without a guard the customer
    # gets the SAME "one small delay" email dozens of times. Key on the failure
    # identity (session OR email+product+reason, since session_id can be missing),
    # backed by an atomic SET-NX so concurrent retries can't both send.
    try:
        from app.core.cache import cache as _alert_cache
        _alert_base = (
            f"{session_id or ''}|{(customer_email or '').strip().lower()}"
            f"|{product_type or ''}|{reason or ''}"
        )
    except Exception:
        _alert_cache = None
        _alert_base = None

    def _alert_should_send(kind: str, ttl: int) -> bool:
        # Fail OPEN (send) if the cache is unavailable — better a duplicate than
        # a silently dropped payment alert.
        if _alert_cache is None or not _alert_base:
            return True
        try:
            key = _alert_cache.cache_key(f"fulfill_alert:{kind}:{_alert_base}")
            return _alert_cache.add(key, {"sent": True}, ttl=ttl)
        except Exception:
            return True

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
        # Ops: at most once per hour per failure identity, so a retry storm
        # doesn't bury the team in 30 identical alerts (they still get re-pinged
        # hourly while the problem persists).
        if _alert_should_send("ops", ttl=3600):
            await EmailService().send_html_email(
                to_email=settings.SUPPORT_EMAIL,
                subject=f"[FULFILLMENT] {reason} ({product_type or '?'})",
                body_html=body_html,
            )
    except Exception as alert_err:
        logger.error(f"[Fulfillment-ALERT] ops email failed: {alert_err}")

    # Customer: exactly once per failure identity for a week — never spam the
    # buyer with repeated "one small delay" notices on every retry.
    if notify_customer and customer_email and _alert_should_send("cust", ttl=604800):
        try:
            await EmailService().send_html_email(
                to_email=customer_email,
                subject="We received your payment — one small delay",
                body_html=branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Thank you for your purchase</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    Your payment for <strong>{(product_type or '').replace('_', ' ').title() or 'your order'}</strong>
                    has been received. We hit a small snag finalising your account and our team
                    has been alerted — we will follow up within a few hours and make sure
                    everything is sorted.
                  </p>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    If you have any questions in the meantime, just reply to this email.
                  </p>
                  <p style="margin:24px 0 0;color:#64748b;font-size:13px;">
                    Order reference: <span style="font-family:monospace;">{session_id or 'n/a'}</span>
                  </p>
                    """,
                    title="We received your payment",
                    preheader="Your payment is received — we're finalising your account.",
                ),
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


def _maybe_fire_cover_sheet(customer_email: str | None, user_id: str | None = None) -> None:
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
    if not customer_email and not user_id:
        return
    db = SessionLocal()
    try:
        from app.core.config import settings
        from app.core.company import COMPANY_DPO_EMAIL
        
        # If the incoming email is the support/DPO fallback, clear it so we force a lookup by ID
        if customer_email in (settings.SUPPORT_EMAIL, COMPANY_DPO_EMAIL):
            logger.warning(f"[_maybe_fire_cover_sheet] Received fallback email {customer_email}, forcing lookup by user_id {user_id}")
            customer_email = None
            
        if not customer_email and user_id:
            u_temp = db.query(User).filter(User.id == user_id).first()
            if u_temp and u_temp.email and u_temp.email not in (settings.SUPPORT_EMAIL, COMPANY_DPO_EMAIL):
                customer_email = u_temp.email

        if not customer_email:
            db.close()
            return

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
        from app.core.models import EvidencePack

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
        from app.services.evidence_enricher import display_legal_name
        company_name = display_legal_name(user, db)
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
        from app.core.models import (
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
                body_html = branded_email_html(
                    f"""
                    <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">You Were Shortlisted</h2>
                    <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">A procurement team is actively evaluating vendors in the <strong>{sector}</strong> sector for a new opportunity.</p>
                    <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your verified status on BOOPPA placed you in the <strong>top 5 shortlisted vendors</strong> for this opportunity.</p>
                    {email_info_box(
                        f"<strong>Opportunity:</strong> {buyer_rfp_title or 'New procurement in your sector'}<br>"
                        f"<strong>Your sector:</strong> {sector}<br>"
                        "<strong>Buyer:</strong> Identity confidential — standard procurement practice",
                        tone="success",
                    )}
                    <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">To improve your position in future shortlists, strengthen your evidence package:</p>
                    {email_button("https://www.booppa.io/vendor/dashboard", "View Dashboard →")}
                    <p style="margin:24px 0 0;color:#64748b;font-size:11px;">
                      You are receiving this because your vendor profile is verified on BOOPPA.<br>
                      Buyer details are kept confidential per procurement best practice.
                    </p>
                    """,
                    title="You were shortlisted",
                    preheader=f"You're in the top 5 shortlisted vendors in {sector}.",
                )
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


