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
        "cover_sheet": True,  # auto-fires once PDPA + RFP + BCEP pack are all ready
    },
}

# Grace window after which the Compliance Evidence Pack cover sheet fires with
# PDPA + RFP only, when the buyer never completed the BCEP evidence-pack intake
# (so the 7-doc pack never reaches status="ready"). Keeps a buyer from being
# left without any cover sheet. See `_maybe_fire_cover_sheet`.
_COVER_SHEET_BCEP_GRACE_DAYS = 7


# ── Cover-sheet subject scoping ───────────────────────────────────────────────
# One account can buy Compliance Evidence Packs for *several different subjects*
# (an admin test harness cycling vendors, a reseller/consultancy buying for
# clients, or the `User.parent_user_id` multi-subsidiary tenancy). Every input
# query in the cover-sheet pipeline used to select "latest row by owner_id",
# which silently mixed subjects: a Netpoleon-branded, blockchain-anchored cover
# sheet carrying Adnovum's PDPA score and findings.
#
# The subject is the **website domain**, matching the trust rule already applied
# to cached identity in `evidence_enricher._cache_trusted_for_hint` ("a
# different site is a different subject"). It is resolved ONCE per cycle and
# threaded through to `fulfill_cover_sheet_task` — never re-derived downstream,
# because two independent resolutions of the same question are exactly how the
# sheet ended up with a correct company name above another company's numbers.


def _as_utc(dt):
    """Treat a naive datetime as UTC. Postgres columns come back naive here, so
    comparing two of them directly is fine, but comparing a naive column value
    against an aware `now()` raises — normalise both through this."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def norm_site(value) -> str:
    """Normalise a website/domain for subject comparison (`https://WWW.X.com/a`
    -> `x.com`). Thin wrapper so callers don't reach into evidence_enricher's
    private helper."""
    from app.services.evidence_enricher import _norm_domain

    return _norm_domain(value)


def resolve_cover_sheet_subject(db, user, latest_pack=None) -> str:
    """The normalised domain this cover-sheet cycle is about, or "" if unknown.

    Priority: the EvidencePack intake's own domain (the buyer stated it for
    *this* purchase), then the newest `rfp_complete` report's website.

    Deliberately does NOT fall back to `User.website`: on a reused account that
    is the *account holder's* site, not the subject's, and a confidently wrong
    subject is worse than an unresolved one — `scope_rows_to_subject` degrades
    safely when the subject is "".
    """
    if latest_pack is not None:
        intake = latest_pack.intake if isinstance(latest_pack.intake, dict) else {}
        site = norm_site(intake.get("domain"))
        if site:
            return site

    if user is not None:
        rfp = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "rfp_complete",
                Report.company_website.isnot(None),
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        if rfp is not None:
            return norm_site(rfp.company_website)
    return ""


def scope_rows_to_subject(rows, target_domain, *, getter=None, include_unknown=False):
    """Filter `rows` down to the ones belonging to `target_domain`.

    Fallback rules — these are what make the scoping fix safe to ship rather
    than a delivery outage:

    * Rows match the subject -> return them. Normal path.
    * No row carries a domain at all -> return everything unfiltered. These are
      legacy rows predating `Report.company_website`; there is no scoping
      information *and* nothing to leak between, so pre-fix behaviour stands.
    * Rows carry domains but none is the subject's -> return []. The account has
      artifacts for other subjects only. Callers MUST defer and alert rather
      than render: a late cover sheet is recoverable, an anchored wrong one is
      not.
    * Subject unresolved ("") -> unfiltered while the account is unambiguous
      (<=1 distinct domain), [] once it is not.

    `include_unknown=True` additionally keeps rows with no domain at all. Use it
    for *index* lists (e.g. DOCUMENTS ANCHORED) where some frameworks
    legitimately never recorded a website — dropping those would strip the ROPA
    and signed cover sheet off the index, which is a worse regression than the
    residual risk of listing an unattributable legacy row. Do NOT use it for
    *selection* (picking the one PDPA scan or RFP kit that backs the sheet's
    headline numbers): there, a wrong pick is the incident itself.
    """
    rows = list(rows)
    getter = getter or (lambda r: getattr(r, "company_website", None))
    target = norm_site(target_domain)

    present = {norm_site(getter(r)) for r in rows}
    present.discard("")

    if target:
        matched = [
            r for r in rows
            if norm_site(getter(r)) == target
            or (include_unknown and not norm_site(getter(r)))
        ]
        if matched:
            return matched
        return rows if not present else []

    return rows if len(present) <= 1 else []


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
    # Normalize `reason` ONCE, up front: callers pass raw exception text, which
    # for a SQLAlchemy error carries newlines, the full SQL statement and a
    # `[parameters: {...}]` tail. That tail changes on every Celery retry (fresh
    # tx_hash, updated_at, presigned URL), so keying the dedupe cache on the raw
    # reason produced a different key each retry and the buyer received the same
    # "one small delay" mail once per attempt. 300 chars keeps the identifying
    # part (exception type + message + start of the statement) and drops the
    # varying tail. Use this value for the log line, subject, body and key.
    reason = " ".join(str(reason or "").split())[:300]

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

    def _alert_release(kind: str) -> None:
        """Drop the dedupe marker so a rejected send is retried next time.

        send_html_email returns False on provider rejection without raising, and
        the marker was already claimed by _alert_should_send — without this the
        ops alert stays suppressed for an hour, the customer notice for a week.
        """
        if _alert_cache is None or not _alert_base:
            return
        try:
            _alert_cache.delete(
                _alert_cache.cache_key(f"fulfill_alert:{kind}:{_alert_base}")
            )
        except Exception:
            pass

    try:
        # `reason` and `extra_str` are exception text going into HTML — escape.
        import html as _html

        reason_html = _html.escape(reason)
        extra_html = _html.escape(extra_str)
        body_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;">
          <h2 style="color:#b91c1c;">Fulfillment alert — manual review needed</h2>
          <p>A Stripe payment landed in a branch that could not complete automatic fulfillment.</p>
          <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <tr><td style="padding:6px 8px;color:#64748b;">Reason</td><td style="padding:6px 8px;"><strong>{reason_html}</strong></td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Product</td><td style="padding:6px 8px;">{product_type or '(unknown)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Customer email</td><td style="padding:6px 8px;">{customer_email or '(missing)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Stripe session</td><td style="padding:6px 8px;font-family:monospace;">{session_id or '(missing)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Webhook event</td><td style="padding:6px 8px;font-family:monospace;">{event_id or '(unknown)'}</td></tr>
            <tr><td style="padding:6px 8px;color:#64748b;">Extra</td><td style="padding:6px 8px;font-family:monospace;">{extra_html or '-'}</td></tr>
          </table>
        </div>
        """
        # Ops: at most once per hour per failure identity, so a retry storm
        # doesn't bury the team in 30 identical alerts (they still get re-pinged
        # hourly while the problem persists).
        if _alert_should_send("ops", ttl=3600):
            _ops_sent = await EmailService().send_html_email(
                to_email=settings.SUPPORT_EMAIL,
                subject=f"[FULFILLMENT] {reason} ({product_type or '?'})",
                body_html=body_html,
            )
            if not _ops_sent:
                logger.error(
                    f"[Fulfillment-ALERT] ops email REJECTED by provider "
                    f"(to={settings.SUPPORT_EMAIL}, reason={reason})"
                )
                _alert_release("ops")
    except Exception as alert_err:
        logger.error(f"[Fulfillment-ALERT] ops email failed: {alert_err}")

    # Customer: exactly once per failure identity for a week — never spam the
    # buyer with repeated "one small delay" notices on every retry.
    if notify_customer and customer_email and _alert_should_send("cust", ttl=604800):
        try:
            _cust_sent = await EmailService().send_html_email(
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
            if not _cust_sent:
                logger.error(
                    f"[Fulfillment-ALERT] customer email REJECTED by provider "
                    f"(to={customer_email}, reason={reason})"
                )
                _alert_release("cust")
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


def _alert_unresolvable_subject(customer_email: str | None, target_domain: str | None, scan_count: int) -> None:
    """Escalate a cover-sheet subject that can never resolve on its own.

    Emitted at ERROR with a stable prefix so a CloudWatch metric filter can alarm
    on it. Deliberately NOT routed through `_alert_payment_fulfillment_issue`:
    that path is async, and `_maybe_fire_cover_sheet` is sync and runs inside a
    Celery task that is itself already under `asyncio.run()` — bridging back with
    another `asyncio.run()` there does not raise, it silently does nothing.

    Rate-limited to one alert per (buyer, subject) per day so an unresolvable
    account escalates once instead of once an hour.
    """
    try:
        from app.core.cache import cache as _c

        key = _c.cache_key(f"cs_unresolvable:{customer_email}:{target_domain or '-'}")
        if not _c.add(key, {"alerted": True}, ttl=86400):
            return
    except Exception:
        pass  # fail open — better a duplicate alert than a silent stall
    logger.error(
        "[CoverSheetStalled] %s cannot resolve a cover-sheet subject (%d completed "
        "scan(s), none matching %r). The hourly sweep re-runs the same check and "
        "will keep deferring indefinitely — needs manual subject resolution.",
        customer_email, scan_count, target_domain or "<unresolved>",
    )


def _maybe_fire_cover_sheet(customer_email: str | None, user_id: str | None = None, test_simulation: bool = False) -> None:
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

    One sheet per SUBJECT, not per account. An account can hold packs for
    several different companies (test harness, reseller, `parent_user_id`
    subsidiaries); each distinct subject is evaluated independently and gets its
    own sheet. Subjects that already have a `compliance_evidence_pack` Report
    are skipped.

    Idempotent — clears `pending_cover_sheet` only once NO subject is still
    waiting, so duplicate calls (any component finishing after another) don't
    re-fire while a second subject's inputs are still in flight.
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
            # A User resolved by primary key IS a real buyer — trust its address
            # unconditionally, even when it equals SUPPORT_EMAIL/COMPANY_DPO_EMAIL.
            # Re-applying the sentinel test here re-rejected the value we had just
            # re-derived, so any account whose own address happens to be the
            # support/DPO address could never be delivered to: the guard blanked
            # the email, looked it up again, saw the same string, and returned.
            # Deterministic, silent, and immune to the hourly sweep.
            u_temp = db.query(User).filter(User.id == user_id).first()
            if u_temp and u_temp.email:
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

        # The BCEP 7-document packs are queried FIRST because their intake
        # carries the subject domain every other lookup must be scoped to. (The
        # pack used to be queried after the PDPA scan, which is how the scan and
        # the name hint could come from two different companies.)
        from app.core.models import EvidencePack
        from app.services.pdpa_findings import resolve_pdpa_score as _resolve_pdpa_score

        packs = (
            db.query(EvidencePack)
            .filter(EvidencePack.user_id == user.id)
            .order_by(EvidencePack.created_at.desc())
            .all()
        )

        # One cover sheet per SUBJECT, not per account. `pending_cover_sheet` is
        # a single boolean, so before this it was also a single slot: a second
        # purchase for a different company either corrupted the first sheet or,
        # once scoping stopped that, silently never got a sheet of its own.
        # Enumerate the distinct subjects this account has bought for (newest
        # first) and skip the ones already delivered.
        subjects: list[tuple[str, object]] = []
        _seen: set[str] = set()
        for _p in packs:
            _intake = _p.intake if isinstance(_p.intake, dict) else {}
            _d = norm_site(_intake.get("domain"))
            if _d in _seen:
                continue
            _seen.add(_d)
            subjects.append((_d, _p))
        if not subjects:
            # No pack rows (e.g. a manually-flagged account) — fall back to the
            # single-subject behaviour this function has always had.
            subjects = [(resolve_cover_sheet_subject(db, user, None), None)]

        # Which subjects already HAVE a sheet. `company_website` on these rows is
        # only populated from the subject-scoping change onward, so rows written
        # before it carry NULL. Matching on domain alone would therefore treat
        # every historical sheet as "never delivered" and re-send the lot — which
        # is exactly what happened: four cover-sheet emails in one minute.
        # Fall back to the company name for those legacy rows, and remember
        # whether any undomained sheet exists at all.
        _cs_rows = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "compliance_evidence_pack",
            )
            .all()
        )
        def _sheets_delivered_since(cycle_start):
            """The already-delivered sets, restricted to sheets issued on or after
            `cycle_start`.

            A cover sheet only counts as "already delivered" for the CYCLE being
            evaluated. The row set above is lifetime, which is correct for the
            one-time `compliance_evidence_pack` SKU (one purchase, one sheet) but
            wrong for `compliance_evidence_monthly`, whose entire proposition is a
            fresh sheet for the SAME subject every month. Unscoped, every cycle
            from the second onward found its subject already in `delivered`,
            skipped it, left `still_waiting` False, and cleared
            `pending_cover_sheet` without ever sending — silently, and beyond the
            reach of the hourly sweep, for the life of the subscription.

            `cycle_start` is this subject's newest EvidencePack timestamp: the
            marker for the current cycle. With no pack row there is no cycle to
            scope to and lifetime behaviour stands, so one-time buyers are
            unaffected.
            """
            if cycle_start is None:
                rows = _cs_rows
            else:
                rows = [
                    r for r in _cs_rows
                    if r.created_at is not None and _as_utc(r.created_at) >= _as_utc(cycle_start)
                ]
            _dom = {norm_site(r.company_website) for r in rows}
            _dom.discard("")
            _names = {
                (r.company_name or "").strip().lower()
                for r in rows
                if not norm_site(r.company_website) and (r.company_name or "").strip()
            }
            # A sheet with neither domain nor usable name — can't be attributed, so
            # treat an unresolved subject as already served rather than re-sending.
            _unattributable = any(
                not norm_site(r.company_website) and not (r.company_name or "").strip()
                for r in rows
            )
            return _dom, _names, _unattributable

        # Subjects fired earlier in THIS pass — prevents the same subject being
        # picked twice before its delivered row exists.
        fired_this_pass: set[str] = set()

        from app.services.evidence_enricher import display_legal_name

        to_fire: list[dict] = []
        still_waiting = False
        # Subjects legitimately accounted for: already delivered for this cycle.
        # Only these justify surrendering `pending_cover_sheet` — see the guard
        # at the end of the loop.
        served_count = 0

        for target_domain, latest_pack in subjects:
            # Scope "already delivered" to THIS subject's current cycle. See
            # `_sheets_delivered_since` — lifetime scoping silently starved every
            # `compliance_evidence_monthly` cycle after the first.
            _cycle_start = getattr(latest_pack, "created_at", None) if latest_pack is not None else None
            delivered, delivered_names, has_unattributable_sheet = _sheets_delivered_since(_cycle_start)

            if target_domain and (target_domain in delivered or target_domain in fired_this_pass):
                served_count += 1
                continue  # this subject already has its cover sheet for this cycle
            _org = (getattr(latest_pack, "organisation", "") or "").strip().lower()
            if _org and _org in delivered_names:
                served_count += 1
                continue  # legacy sheet (no domain recorded) for this same org
            if not target_domain and (delivered_names or has_unattributable_sheet):
                # Unresolved subject on an account that already holds a sheet —
                # cannot prove this is a *different* subject, so don't re-send.
                served_count += 1
                continue

            # At most ONE sheet per invocation. Firing every pending subject in
            # a single pass sends N emails at once, and the task's content-aware
            # 24h email guard cannot collapse them: each subject legitimately
            # has a different anchored-document fingerprint, so each gets its
            # own dedupe key. (test_simulation runs bypass that guard entirely.)
            # `still_waiting` keeps the flag set, so the hourly sweep takes the
            # next subject — by which time this one has written its delivered
            # row and cannot be picked twice.
            if to_fire:
                still_waiting = True
                break

            # Cover-sheet readiness must reflect a *deliverable* PDPA scan,
            # using the same guard the render path applies (forensic finding: an
            # empty-score artifact — "Vendor: Test", suite-b.booppa.io, all
            # scores "—" — was bundled into a paying customer's pack). Take the
            # newest completed scan that has a real, resolvable score — not the
            # oldest row, and not a stub the render path would then reject.
            _pdpa_candidates = (
                db.query(Report)
                .filter(
                    Report.owner_id == user.id,
                    Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                    Report.status == "completed",
                )
                .order_by(Report.created_at.desc())
                .limit(25)
                .all()
            )
            # Scope to THIS subject before picking a winner. Without this, a
            # later scan for a different company wins the created_at race and
            # its score lands on this sheet.
            _scoped_pdpa = scope_rows_to_subject(_pdpa_candidates, target_domain)
            if _pdpa_candidates and not _scoped_pdpa:
                # Scans exist, but none for this subject. Not "not ready yet" —
                # ambiguous. Keep waiting rather than render a mixed sheet.
                logger.warning(
                    "[CoverSheet] %s has %d completed scan(s), none for subject %r "
                    "— deferring rather than rendering another subject's data",
                    customer_email, len(_pdpa_candidates),
                    target_domain or "<unresolved>",
                )
                # Deferring is correct — an anchored wrong sheet is unrecoverable.
                # But this condition cannot clear itself: the sweep re-runs the
                # identical query hourly and gets the identical answer, so without
                # an escalation it spins forever at WARNING level and nobody is
                # paged. Alert once per subject per day, then keep deferring.
                _alert_unresolvable_subject(
                    customer_email, target_domain, len(_pdpa_candidates),
                )
                still_waiting = True
                continue

            pdpa_report = None
            for _cand in _scoped_pdpa:
                _cad = _cand.assessment_data if isinstance(_cand.assessment_data, dict) else {}
                if _resolve_pdpa_score(_cad) is None:
                    continue  # empty-score scan — not a deliverable
                pdpa_report = _cand
                break

            # RFP readiness, per subject. `compliance_evidence_rfp_ready` is
            # another per-account boolean, so it can only be trusted when the
            # account has a single subject; otherwise require this subject's own
            # completed kit.
            _rfp_rows = (
                db.query(Report)
                .filter(
                    Report.owner_id == user.id,
                    Report.framework == "rfp_complete",
                    Report.status == "completed",
                )
                .order_by(Report.created_at.desc())
                .all()
            )
            rfp_done = bool(scope_rows_to_subject(_rfp_rows, target_domain))
            if not rfp_done and len(subjects) == 1:
                rfp_done = bool(getattr(user, "compliance_evidence_rfp_ready", False))

            if not (pdpa_report is not None and rfp_done):
                still_waiting = True
                continue

            # The BCEP 7-document pack is the third input. It generates only
            # after the buyer completes the (separate) evidence-pack intake, so
            # it is usually the last to finish — wait for it unless the grace
            # window has elapsed (buyer never completed the intake), so nobody
            # is left without a cover sheet. Only *wait* when a non-ready pack
            # row actually exists.
            if latest_pack is not None and latest_pack.status != "ready":
                pdpa_age_ok = False
                if pdpa_report.created_at is not None:
                    created = pdpa_report.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    pdpa_age_ok = (
                        datetime.now(timezone.utc) - created
                    ) > timedelta(days=_COVER_SHEET_BCEP_GRACE_DAYS)
                if not pdpa_age_ok:
                    still_waiting = True
                    continue
                logger.info(
                    "[CoverSheet] BCEP pack still not ready after %d-day grace for "
                    "%s / %s — firing cover sheet with PDPA + RFP only",
                    _COVER_SHEET_BCEP_GRACE_DAYS, customer_email,
                    target_domain or "<unresolved>",
                )

            # Hint the resolver with the entity THIS sheet's inputs were issued
            # to. Without a hint `_cache_trusted_for_hint` returns True and the
            # sticky legal_name cache is silently trusted, which is how one
            # company's name lands on another's certified document (the SPQR
            # leak shape). The website hint matters independently: trading names
            # collide, so a matching company string alone is not enough.
            _pack_intake = (
                latest_pack.intake
                if latest_pack is not None and isinstance(getattr(latest_pack, "intake", None), dict)
                else {}
            )
            cover_hint = (
                (getattr(latest_pack, "organisation", "") or "").strip()
                or (getattr(pdpa_report, "company_name", "") or "").strip()
                or (_pack_intake.get("org_name") or "").strip()
                or (getattr(user, "company", "") or "").strip()
                or None
            )
            logger.info(
                "[CoverSheet] cover_hint=%r for %s / %s "
                # Key spelled `user_company` not `user.company`: the rename-path
                # guard in test_identity_write_path_invariant.py scans source text
                # for `user.company =`, and a format string matches it.
                "(pack.organisation=%r, pdpa.company_name=%r, intake.org_name=%r, user_company=%r)",
                cover_hint, customer_email, target_domain or "<unresolved>",
                getattr(latest_pack, "organisation", None),
                getattr(pdpa_report, "company_name", None),
                _pack_intake.get("org_name"),
                getattr(user, "company", None),
            )
            to_fire.append({
                "target_domain": target_domain or None,
                "company_name": display_legal_name(
                    user, db, company_hint=cover_hint,
                    website_hint=target_domain or None,
                ),
            })
            if target_domain:
                fired_this_pass.add(target_domain)

        # Only surrender the flag when nothing is left outstanding — otherwise
        # the hourly sweep must keep re-checking for the subjects still waiting.
        #
        # `pending_cover_sheet` must NEVER go False on a pass that neither queued
        # a sheet nor accounted for every subject as already-served this cycle.
        # Clearing it removes the buyer from the sweep's query, so a wrong clear
        # is terminal and silent — this is how the monthly-cycle bug stayed
        # invisible, and the same shape would hide any future skip added to the
        # loop. Keeping the flag costs one cheap re-check an hour; dropping it
        # costs the customer the document they paid for.
        if not still_waiting and (to_fire or served_count == len(subjects)):
            user.pending_cover_sheet = False
        elif not still_waiting:
            logger.error(
                "[CoverSheetStalled] %s: %d subject(s), %d already served, none "
                "queued — refusing to clear pending_cover_sheet (would drop the "
                "buyer from the sweep with no sheet delivered)",
                customer_email, len(subjects), served_count,
            )
        db.commit()
    finally:
        db.close()

    for _job in to_fire:
        try:
            from app.workers.tasks import fulfill_cover_sheet_task

            fulfill_cover_sheet_task.apply_async(
                kwargs={
                    "bundle_type": "compliance_evidence_pack",
                    "customer_email": customer_email,
                    "company_name": _job["company_name"],
                    # The subject is resolved ONCE, here, and threaded through.
                    # The task must not re-derive it — an independent second
                    # resolution is how the sheet got a correct company name
                    # above another company's score.
                    "target_domain": _job["target_domain"],
                    # test_simulation flows into the task so its anchor is
                    # mocked (no gas) for admin test-checkout runs.
                    "metadata": {"auto_fired": True, "test_simulation": bool(test_simulation)},
                },
                countdown=10,
            )
            logger.info(
                "[CoverSheet] Auto-fired for %s / %s (all components ready)",
                customer_email, _job["target_domain"] or "<unresolved>",
            )
        except Exception as e:
            logger.warning(
                "[CoverSheet] Auto-fire failed for %s / %s: %s",
                customer_email, _job["target_domain"], e,
            )


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
                if not await email_svc.send_html_email(
                    to_email=user.email,
                    subject="You Were Shortlisted — New Procurement Opportunity",
                    body_html=body_html,
                ):
                    logger.error(
                        f"[Strategy6] Shortlist email REJECTED by provider for {user.email}"
                    )
                else:
                    logger.info(
                        f"[Strategy6] Notified vendor {user.email} for sector {sector}"
                    )
            except Exception as e:
                logger.warning(f"[Strategy6] Email failed for {user.email}: {e}")

    except Exception as e:
        logger.error(f"[Strategy6] Failed: {e}")
    finally:
        db.close()


