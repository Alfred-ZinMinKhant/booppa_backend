from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service

from app.services.fulfillment.helpers import (
    _create_stub_report,
    _alert_payment_fulfillment_issue,
    _maybe_fire_cover_sheet,
    _fire_strategy_6,
)
from app.services.fulfillment.single_products import _defer_rfp_to_intake

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
            from app.services.csp_access import deliver_csp_activation

            # Activates the org AND queues the Day-1 Registration Readiness
            # Baseline, which sends the single activation+artifact email. Shared
            # with the monthly path in subscriptions.py — do not re-add a bare
            # activation email here or the buyer gets two messages.
            await deliver_csp_activation(
                db,
                user=user,
                plan="csp",
                billing_type="one_time",
                metadata=metadata,
                session_id=session_id,
                test_simulation=bool(metadata.get("test_simulation")),
            )
            logger.info(f"[CSP] One-time pack access granted to {customer_email}")
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
                # Do NOT fall back to the account's cached legal identity here. The
                # subject of a PDPA/Vendor Proof purchase is whatever the buyer
                # entered at checkout (stripe_checkout requires company_name for both
                # SKUs); resolving from user.legal_name is exactly how a reused
                # account's stale identity contaminated a different vendor's report.
                # If company_name is somehow empty, leave it empty and let the stub
                # Report's own subject drive report-scoped resolution downstream.
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
                    body_html=branded_email_html(
                        f"""
                      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Notarization credits issued</h2>
                      <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                        Thanks for your purchase. You now have
                        <strong>{count} notarization credit{'s' if count != 1 else ''}</strong>
                        on your account. Each lets you anchor any compliance document (PDF, DOCX, image, etc.)
                        on the blockchain with SHA-256 proof.
                      </p>
                      {email_info_box(
                          '<strong>How to redeem</strong><br>'
                          'Visit <a href="https://www.booppa.io/notarize" style="color:#10b981;font-weight:bold;">booppa.io/notarize</a>, '
                          f'upload your document, and enter this email ({customer_email}). '
                          'Your credit will be applied automatically — no payment required.'
                      )}
                      <p style="margin:0;color:#64748b;font-size:13px;">
                        Credits don't expire. You can use them one at a time or all at once.
                      </p>
                        """,
                        title="Notarization credits issued",
                        preheader=f"You now have {count} notarization credit(s) to redeem.",
                    ),
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
        raise
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
    from app.core.models import EvidencePack

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
            # Marks the pack as an admin test-checkout run so fulfill_evidence_pack_task
            # mocks every anchor (no gas). Reliable even when session_id is None
            # (the compliance_evidence_monthly subscription cycle).
            "test_simulation": True,
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
        from app.services.email_templates import get_evidence_pack_intake_html
        body_html = get_evidence_pack_intake_html(intake_url)
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
    from app.core.models import PendingRfpIntake

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
                    user.compliance_evidence_rfp_ready = False
                    logger.info(
                        f"[Bundle:compliance_evidence_pack] Set CE credit=1 for {customer_email} "
                        f"(was {current_ce}); reset signed_cover_sheet_uploaded=False, rfp_ready=False for fresh cycle"
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
            brief_cta = email_info_box(
                f'<strong>One step to unlock your {kit_label}</strong><br>'
                "Share a few details about the procurement (about 2 minutes) and we'll "
                "generate the kit.", tone="warn",
            ) + email_button(intake_url, "Complete your RFP brief →")

        if (sections or brief_cta) and customer_email:
            bundle_label = product_type.replace('_', ' ').title()
            try:
                sent = await EmailService().send_html_email(
                    to_email=customer_email,
                    subject=f"Your {bundle_label} — what's included & next steps",
                    body_html=branded_email_html(
                        f"""
                      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your {bundle_label} is being prepared</h2>
                      <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Here's everything included and what happens next:</p>
                      {''.join(sections)}
                      {brief_cta}
                      <p style="color:#64748b;font-size:13px;margin:20px 0 0;">booppa.io</p>
                        """,
                        title=f"Your {bundle_label}",
                        preheader=f"Your {bundle_label} is being prepared — here's what's next.",
                    ),
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
        raise
    finally:
        db.close()


