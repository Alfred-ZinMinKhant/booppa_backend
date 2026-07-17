from app.core.route_classes import RetryAPIRoute
from fastapi import APIRouter, Request, HTTPException, Depends, Security
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.auth import verify_access_token
from app.core.config import settings
from fastapi.security import OAuth2PasswordBearer
import os
import stripe
import logging

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RetryAPIRoute)
_limiter = Limiter(key_func=get_remote_address)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


# Price map: resolved at request time so env vars set after import are picked up.
# Products that share a Stripe Price with another SKU when they have no
# dedicated price-ID env var of their own. `notarization_addon_1` is a one-time
# single-credit top-up — the same deliverable and price as
# `compliance_notarization_1` — but has no `STRIPE_NOTARIZATION_ADDON_1` var in
# most environments, so it falls back to the notarization single-credit price
# instead of 400ing at checkout for exactly its target buyers.
_PRICE_FALLBACKS = {
    "notarization_addon_1": "compliance_notarization_1",
}


def _get_price(product_type: str) -> str | None:
    """Look up the Stripe Price ID for a product at call time (not import time)."""
    env_key = product_type.upper()
    price = os.environ.get(f"STRIPE_{env_key}") or os.environ.get(
        f"NEXT_PUBLIC_STRIPE_{env_key}"
    )
    if not price and product_type in _PRICE_FALLBACKS:
        alias = _PRICE_FALLBACKS[product_type].upper()
        price = os.environ.get(f"STRIPE_{alias}") or os.environ.get(
            f"NEXT_PUBLIC_STRIPE_{alias}"
        )
    return price


MODE_MAP = {
    # One-time products
    "pdpa_quick_scan": "payment",
    "vendor_proof": "payment",
    "rfp_complete": "payment",
    "compliance_notarization_1": "payment",
    # One-time top-up: 1 extra notarization credit.
    "notarization_addon_1": "payment",
    # Batch notarization tiers are recurring monthly allowances.
    "compliance_notarization_10": "subscription",
    "compliance_notarization_50": "subscription",
    # Bundles (one-time)
    "vendor_trust_pack": "payment",
    "rfp_accelerator": "payment",
    "enterprise_bid_kit": "payment",
    "compliance_evidence_pack": "payment",
    # Subscriptions
    "vendor_active_monthly": "subscription",
    "vendor_active_annual": "subscription",
    "pdpa_monitor_monthly": "subscription",
    "pdpa_monitor_annual": "subscription",
    "compliance_evidence_monthly": "subscription",
    # `enterprise_monthly`, `enterprise_pro_monthly`, `evaluate_suppliers_monthly`,
    # and `verify_supplier_evidence_monthly` are retired — no longer listed on any
    # pricing tile. Downstream maps (webhook entitlement, billing enforcement,
    # score bonuses) still resolve them so existing subscribers keep working;
    # removing them here just prevents NEW checkouts.
    "standard_suite_monthly": "subscription",
    "pro_suite_monthly": "subscription",
    "tender_intelligence_monthly": "subscription",
    "tender_intelligence_annual": "subscription",
    "vendor_pro_monthly": "subscription",
    "vendor_pro_annual": "subscription",
    # Buyer ladder (replaces legacy evaluate_suppliers / verify_supplier_evidence
    # — those keys above are retained for backward-compat with existing subs).
    "buyer_starter_monthly": "subscription",
    "buyer_starter_annual": "subscription",
    "buyer_pro_monthly": "subscription",
    "buyer_pro_annual": "subscription",
    "buyer_enterprise_monthly": "subscription",
    "buyer_enterprise_annual": "subscription",
    # CSP Compliance Pack. Full + Monitoring are recurring; one-time grants
    # lifetime pack access (no recurring billing).
    "csp_pack_monthly": "subscription",
    "csp_monitoring_monthly": "subscription",
    "csp_pack_onetime": "payment",
}


# Block vendors from purchasing procurement-only plans.
# Standard/Pro Suites are role-agnostic compliance infrastructure — vendors
# (incl. enterprise vendors) can buy them. Only the explicit supplier-evaluation
# SKUs are gated to procurement accounts.
PROCUREMENT_PRODUCTS = {
    # Buyer ladder — vendors cannot buy buyer-side plans.
    # (Retired enterprise_monthly / enterprise_pro_monthly / evaluate_suppliers_monthly
    # / verify_supplier_evidence_monthly are no longer in MODE_MAP, so the
    # gate doesn't need to list them.)
    "buyer_starter_monthly", "buyer_starter_annual",
    "buyer_pro_monthly", "buyer_pro_annual",
    "buyer_enterprise_monthly", "buyer_enterprise_annual",
}


async def _gate_acra_live(uen: str) -> None:
    """Pre-payment ACRA gate for Vendor Proof purchases.

    Looks the UEN up in the live data.gov.sg ACRA dataset and blocks the
    checkout when the entity is not found or not active (struck-off / ceased).
    Vendor Proof attests an active entity; selling one for a dead UEN produces a
    certificate no procurement officer will trust. A lookup *error* (network /
    dataset hiccup) is non-fatal — we let the purchase proceed and fall back to
    the post-payment warning rather than block a paying customer on our outage.
    """
    try:
        from app.services.evidence_enricher import fetch_acra_status

        status = await fetch_acra_status(uen)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[checkout] ACRA gate lookup failed for UEN %s: %s", uen, e)
        return

    if not status.get("found"):
        raise HTTPException(
            status_code=422,
            detail="UEN not found in the ACRA registry — verify your business registration number.",
        )
    if not status.get("live"):
        es = status.get("entity_status") or "not active"
        raise HTTPException(
            status_code=409,
            detail=(
                f"This UEN is registered as {es} — Vendor Proof is available only "
                "for active (Live) entities."
            ),
        )


def get_stripe_client():
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    stripe.api_key = secret
    return stripe


def get_base_url():
    # frontend base to redirect back to after checkout
    return (
        os.environ.get("NEXT_PUBLIC_BASE_URL")
        or os.environ.get("BACKEND_BASE_URL")
        or os.environ.get("NEXT_PUBLIC_API_BASE")
        or "http://localhost:3000"
    )


@router.post("/checkout")
@_limiter.limit("20/minute")
async def checkout_post(request: Request, token: str | None = Security(oauth2_scheme)):
    """Create a Stripe Checkout session. Requires an authenticated user.

    Body: { productType, priceId (optional), prefill_email (optional) }
    """
    payload = verify_access_token(token) if token else None
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Please sign in to purchase.")
    auth_email = payload.get("sub")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    product_type = data.get("productType") or data.get("product_type")
    price_id = data.get("priceId") or data.get("price_id")
    report_id = data.get("reportId") or data.get("report_id")
    prefill_email = (
        data.get("prefill_email")
        or data.get("prefillEmail")
        or data.get("customerEmail")
        or data.get("customer_email")
        or auth_email
    )

    if not product_type and not price_id:
        raise HTTPException(status_code=400, detail="Missing productType or priceId")

    if product_type in PROCUREMENT_PRODUCTS and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            if user and getattr(user, "role", "VENDOR") == "VENDOR":
                raise HTTPException(
                    status_code=403,
                    detail="This plan is for procurement teams. Switch to a procurement account or visit /solutions/vendors for vendor tools.",
                )
        finally:
            _db.close()

    # Block re-purchase of an already-active subscription
    SUBSCRIPTION_PLAN_MAP = {
        "vendor_active_monthly": "vendor_active",
        "vendor_active_annual": "vendor_active",
        "pdpa_monitor_monthly": "pdpa_monitor",
        "pdpa_monitor_annual": "pdpa_monitor",
        "compliance_evidence_monthly": "compliance_evidence",
        # Retired plans kept in the map so we still recognise their tier when
        # an existing subscriber upgrades/downgrades through Stripe portal.
        "evaluate_suppliers_monthly": "evaluate_suppliers",
        "verify_supplier_evidence_monthly": "verify_supplier_evidence",
        "enterprise_monthly": "enterprise",
        "enterprise_pro_monthly": "enterprise_pro",
        "standard_suite_monthly": "standard_suite",
        "pro_suite_monthly": "pro_suite",
        "tender_intelligence_monthly": "tender_intelligence",
        "tender_intelligence_annual": "tender_intelligence",
        "vendor_pro_monthly": "vendor_pro",
        "vendor_pro_annual": "vendor_pro",
        # Buyer ladder — monthly + annual share the same plan family so
        # the duplicate-active-subscription guard treats them as one.
        "buyer_starter_monthly": "buyer_starter",
        "buyer_starter_annual": "buyer_starter",
        "buyer_pro_monthly": "buyer_pro",
        "buyer_pro_annual": "buyer_pro",
        "buyer_enterprise_monthly": "buyer_enterprise",
        "buyer_enterprise_annual": "buyer_enterprise",
        # CSP Compliance Pack subscriptions (one-time grant handled separately).
        "csp_pack_monthly": "csp",
        "csp_monitoring_monthly": "csp_monitoring",
    }
    if product_type in SUBSCRIPTION_PLAN_MAP and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            if user:
                expected_plan = SUBSCRIPTION_PLAN_MAP[product_type]
                # Check Subscription table for active sub of the same type
                from app.core.models import Subscription as SubModel
                plan_keys = [
                    k for k, v in SUBSCRIPTION_PLAN_MAP.items()
                    if v == expected_plan
                ]
                from app.core.repositories.subscription_repository import SubscriptionRepository
                active_sub = SubscriptionRepository.get_active_by_user_and_products(_db, str(user.id), plan_keys)
                if active_sub:
                    # double-check with Stripe if we have a customer id to avoid stale local state
                    try:
                        stripe_client = get_stripe_client()
                        cust_id = getattr(user, "stripe_customer_id", None)
                        if cust_id:
                            subs = stripe_client.Subscription.list(
                                customer=cust_id, limit=10
                            )
                            has_active = any(
                                s.get("status") in ("active", "trialing")
                                for s in getattr(subs, "data", subs.get("data", []))
                            )
                            if has_active:
                                raise HTTPException(
                                    status_code=409,
                                    detail=f"You already have an active {expected_plan.replace('_', ' ').title()} subscription. Manage it from your subscription page.",
                                )
                            # If Stripe shows no active subscriptions, allow checkout to continue and let webhook reconcile
                    except HTTPException:
                        raise
                    except Exception:
                        # If Stripe check fails, fall back to local block to be conservative
                        raise HTTPException(
                            status_code=409,
                            detail=f"You already have an active {expected_plan.replace('_', ' ').title()} subscription. Manage it from your subscription page.",
                        )
                else:
                    # No local Subscription row — but the webhook may have failed
                    # to write it. Ask Stripe directly: any customer with this
                    # email holding an active sub means they already paid.
                    try:
                        stripe_client = get_stripe_client()
                        customers = stripe_client.Customer.list(
                            email=prefill_email, limit=5
                        )
                        for cust in getattr(customers, "data", customers.get("data", [])):
                            subs = stripe_client.Subscription.list(
                                customer=cust.get("id"), limit=10
                            )
                            for s in getattr(subs, "data", subs.get("data", [])):
                                if s.get("status") not in ("active", "trialing"):
                                    continue
                                # Match the price against the configured price for this product family.
                                expected_keys = {
                                    k.upper() for k in plan_keys
                                }
                                expected_prices = {
                                    os.environ.get(f"STRIPE_{k}") for k in expected_keys
                                } | {
                                    os.environ.get(f"NEXT_PUBLIC_STRIPE_{k}") for k in expected_keys
                                }
                                expected_prices.discard(None)
                                for item in (s.get("items", {}).get("data") or []):
                                    price_id = (item.get("price") or {}).get("id")
                                    if price_id and price_id in expected_prices:
                                        raise HTTPException(
                                            status_code=409,
                                            detail=f"You already have an active {expected_plan.replace('_', ' ').title()} subscription. Manage it from your subscription page.",
                                        )
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.warning(f"[Checkout] Stripe-by-email guard check failed: {e}")
        finally:
            _db.close()

    # For PDPA Monitor: ensure we have a website URL for the initial scan.
    # Pull from user profile; if missing, require it in the request body and save it.
    if product_type in ("pdpa_monitor_monthly", "pdpa_monitor_annual") and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            website = (getattr(user, "website", "") or "").strip() if user else ""
            if not website:
                website = (data.get("website") or data.get("vendor_url") or "").strip()
                # Save to profile so the webhook can read it for the initial scan
                if website and user:
                    user.website = website
                    _db.commit()
            if not website:
                raise HTTPException(
                    status_code=422,
                    detail="A website URL is required for PDPA Monitor so we can run your first scan. Please add your website to your profile or provide it during checkout.",
                )
        finally:
            _db.close()

    # Standalone PDPA Quick Scan from /pricing: needs website + company_name so the
    # webhook can create a stub Report and run the scan + email the certificate.
    if product_type == "pdpa_quick_scan" and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            req_website = (data.get("website") or data.get("vendor_url") or "").strip()
            req_company = (data.get("company_name") or "").strip()
            website = req_website or ((getattr(user, "website", "") or "").strip() if user else "")
            company_name = req_company or ((getattr(user, "company", "") or "").strip() if user else "")
            if user:
                if req_website and not user.website:
                    user.website = req_website
                if req_company and not user.company:
                    user.company = req_company
                if req_website or req_company:
                    _db.commit()
            if not website:
                raise HTTPException(
                    status_code=422,
                    detail="A website URL is required so we can run your PDPA scan. Please provide your website.",
                )
            if not company_name:
                raise HTTPException(
                    status_code=422,
                    detail="A company name is required for the PDPA scan certificate. Please provide your company name.",
                )
            data["vendor_url"] = website
            data["company_name"] = company_name
        finally:
            _db.close()

    # Standalone Vendor Proof from /pricing: needs company_name for the verification
    # record + badge. Website is optional (verification is on the entity, not the site).
    if product_type == "vendor_proof" and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            req_company = (data.get("company_name") or "").strip()
            req_website = (data.get("website") or data.get("vendor_url") or "").strip()
            company_name = req_company or ((getattr(user, "company", "") or "").strip() if user else "")
            website = req_website or ((getattr(user, "website", "") or "").strip() if user else "")
            if user:
                if req_company and not user.company:
                    user.company = req_company
                if req_website and not user.website:
                    user.website = req_website
                if req_company or req_website:
                    _db.commit()
            if not company_name:
                raise HTTPException(
                    status_code=422,
                    detail="A company name is required to issue your Vendor Proof. Please provide your company name.",
                )
            data["company_name"] = company_name
            if website:
                data["vendor_url"] = website

            # Pre-payment ACRA gate: when a UEN is supplied, block struck-off /
            # ceased / not-found entities BEFORE charging (the post-payment webhook
            # can only warn). UEN stays optional — buyers without one are unaffected.
            req_uen = (data.get("uen") or "").strip()
            uen = req_uen or ((getattr(user, "uen", "") or "").strip() if user else "")
            
            if not uen and company_name:
                from app.services.evidence_enricher import fetch_acra_status
                acra_res = await fetch_acra_status(company_name=company_name)
                if acra_res and acra_res.get("found") and acra_res.get("uen"):
                    uen = acra_res.get("uen")

            if uen:
                await _gate_acra_live(uen)
                data["uen"] = uen
                if user and not getattr(user, "uen", None):
                    user.uen = uen
                    _db.commit()
        finally:
            _db.close()

    # For bundles: require website (for VP + PDPA scan) and company name.
    # Falls back to user profile, otherwise 422 to trigger frontend prompt.
    BUNDLE_TYPES = {
        "vendor_trust_pack", "rfp_accelerator",
        "enterprise_bid_kit", "compliance_evidence_pack",
        "rfp_express", "rfp_complete",
    }
    if product_type in BUNDLE_TYPES:
        from app.core.db import SessionLocal
        from app.core.models import User

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email) if prefill_email else None
            req_website = (data.get("website") or data.get("vendor_url") or "").strip()
            req_company = (data.get("company_name") or "").strip()
            website = req_website or ((getattr(user, "website", "") or "").strip() if user else "")
            company_name = req_company or ((getattr(user, "company", "") or "").strip() if user else "")
            # Save back to profile so future bundle/scan flows can reuse
            if user:
                if req_website and not user.website:
                    user.website = req_website
                if req_company and not user.company:
                    user.company = req_company
                if req_website or req_company:
                    _db.commit()
            if not website:
                raise HTTPException(
                    status_code=422,
                    detail="A website URL is required so we can run the PDPA scan and Vendor Proof check included in this bundle. Please provide your website.",
                )
            if not company_name:
                raise HTTPException(
                    status_code=422,
                    detail="A company name is required for this bundle. Please provide your company name.",
                )
            # Inject into data so the metadata block below picks them up
            data["vendor_url"] = website
            data["company_name"] = company_name

            # These bundles include a Vendor Proof component — apply the same
            # optional pre-payment ACRA gate when a UEN is supplied.
            req_uen = (data.get("uen") or "").strip()
            uen = req_uen or ((getattr(user, "uen", "") or "").strip() if user else "")
            
            if not uen and company_name:
                from app.services.evidence_enricher import fetch_acra_status
                acra_res = await fetch_acra_status(company_name=company_name)
                if acra_res and acra_res.get("found") and acra_res.get("uen"):
                    uen = acra_res.get("uen")

            if uen:
                await _gate_acra_live(uen)
                data["uen"] = uen
                if user and not getattr(user, "uen", None):
                    user.uen = uen
                    _db.commit()
        finally:
            _db.close()

    # Block vendor_proof purchase if user is already verified
    if product_type == "vendor_proof" and prefill_email:
        from app.core.db import SessionLocal
        from app.core.models import User
        from app.core.models import VerifyRecord, LifecycleStatus

        _db = SessionLocal()
        try:
            from app.core.repositories.user_repository import UserRepository
            user = UserRepository.get_by_email(_db, prefill_email)
            if user:
                from app.core.repositories.verify_record_repository import VerifyRecordRepository
                already_verified = VerifyRecordRepository.active_exists_by_vendor_id(_db, str(user.id))
                if already_verified:
                    raise HTTPException(
                        status_code=409,
                        detail="You are already verified. No need to purchase Vendor Proof again.",
                    )
        finally:
            _db.close()

    if not price_id:
        price_id = _get_price(product_type)

    if not price_id:
        env_key = (product_type or "").upper()
        logger.error(
            f"No Stripe price found for product_type={product_type!r}. "
            f"Checked STRIPE_{env_key}={os.environ.get(f'STRIPE_{env_key}')!r} "
            f"and NEXT_PUBLIC_STRIPE_{env_key}={os.environ.get(f'NEXT_PUBLIC_STRIPE_{env_key}')!r}"
        )
        raise HTTPException(
            status_code=400, detail="Invalid product type or price not configured"
        )

    mode = MODE_MAP.get(product_type, "payment")

    client_ip = request.client.host if request.client else "unknown"

    try:
        stripe_client = get_stripe_client()
        base_url = get_base_url()
        # Log if the resolved price_id is empty to aid debugging
        if not price_id:
            logger.warning(
                f"Stripe price for product '{product_type}' is not configured. Checked env vars for product mapping."
            )
        logger.info(
            f"Creating checkout session for product={product_type} price_id={price_id} report_id={report_id} prefill_email={prefill_email}"
        )
        success_url = f"{base_url}/thank-you?session_id={{CHECKOUT_SESSION_ID}}&product={product_type}"
        # Notarization products redirect to the certificate result page
        if product_type and "compliance_notarization" in product_type and report_id:
            success_url = f"{base_url}/notarization/result?session_id={{CHECKOUT_SESSION_ID}}&report_id={report_id}"
        elif product_type and "rfp_" in product_type:
            success_url = (
                f"{base_url}/rfp-acceleration/result?session_id={{CHECKOUT_SESSION_ID}}"
            )
        # simple cancel URL mapping
        _pt = product_type or ""
        if _pt in ("vendor_trust_pack", "rfp_accelerator", "enterprise_bid_kit"):
            cancel_path = "pricing"
        elif "pdpa" in _pt:
            cancel_path = "pdpa"
        elif "compliance_notarization" in _pt:
            cancel_path = "notarization"
        elif "supply_chain" in _pt:
            cancel_path = "supply-chain"
        elif "rfp_" in _pt:
            cancel_path = "rfp-acceleration"
        elif _pt == "vendor_proof":
            cancel_path = "vendor-proof"
        elif _pt in ("vendor_active_monthly", "vendor_active_annual"):
            cancel_path = "pricing"
        elif _pt in ("pdpa_monitor_monthly", "pdpa_monitor_annual"):
            cancel_path = "pricing"
        else:
            cancel_path = "pricing"
        cancel_url = f"{base_url}/{cancel_path}"

        vendor_url = data.get("vendor_url", "")
        company_name = data.get("company_name", "")
        rfp_description = data.get("rfp_description", "")
        uen = (data.get("uen") or "").strip()
        intake_data = data.get("intake_data") or {}  # dict of buyer-supplied facts

        metadata = {"product_type": product_type or "", "client_ip": client_ip}
        if report_id:
            metadata["report_id"] = str(report_id)
        # include customer email in metadata when provided
        if prefill_email:
            metadata["customer_email"] = prefill_email
        # RFP fulfillment fields — required by webhook to generate the package
        if vendor_url:
            metadata["vendor_url"] = vendor_url
        if company_name:
            metadata["company_name"] = company_name
        # ACRA-gated UEN (Vendor Proof / bundles) — flows to the fulfillment
        # webhook so the certificate states the verified registration number.
        if uen:
            metadata["uen"] = uen
        if rfp_description:
            metadata["rfp_description"] = rfp_description
        # Buyer-supplied facts indicator
        if intake_data:
            metadata["has_intake"] = "1"

        # Use automatic_payment_methods so Stripe shows whatever is enabled in
        # the dashboard (card, PayNow, etc.) without hardcoding method names.
        # Subscriptions don't support automatic_payment_methods, so fall back to card only.
        _session_kwargs: dict = dict(
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            client_reference_id=str(report_id) if report_id else None,
            customer_email=prefill_email if prefill_email else None,
            allow_promotion_codes=True,
        )
        if mode != "payment":
            _session_kwargs["payment_method_types"] = ["card"]
        session = stripe_client.checkout.Session.create(**_session_kwargs)
        logger.info(
            f"Created Stripe session id={getattr(session, 'id', None)} url={getattr(session, 'url', None)} metadata={metadata}"
        )

        # Record CHECKOUT funnel event (non-blocking)
        try:
            from app.core.db import SessionLocal as _SL
            from app.services.funnel_analytics import record_funnel_event

            _fdb = _SL()
            record_funnel_event(
                _fdb,
                stage="CHECKOUT",
                session_id=getattr(session, "id", None),
                source="stripe",
                metadata={"product_type": product_type},
            )
            _fdb.commit()
            _fdb.close()
        except Exception:
            pass  # never break checkout

        # Cache pre-checkout intake (description + supplied facts) so the
        # post-payment /rfp-intake/{id} form can pre-populate. Buyer still
        # confirms/edits before final submission — cache is a UX nicety, not
        # the source of truth for what gets anchored.
        if hasattr(session, "id") and (intake_data or rfp_description):
            from app.core.cache import cache as cache_mod

            cache_mod.set(
                cache_mod.cache_key(f"rfp_intake:{session.id}"),
                {
                    "rfp_description": rfp_description or "",
                    "intake_data": intake_data or {},
                },
                ttl=86400,  # 24 hours — well within the brief-completion window
            )

        return JSONResponse({"url": session.url})
    except RuntimeError as e:
        logger.error(f"Stripe configuration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create Stripe session")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/checkout")
@_limiter.limit("20/minute")
async def checkout_get(
    product: str | None = None,
    prefill_email: str | None = None,
    request: Request = None,
):
    """Support GET requests like /checkout?product=... to create a session and redirect the user to Stripe."""
    product_type = product
    price_id = _get_price(product_type) if product_type else None

    if not price_id:
        raise HTTPException(status_code=404, detail="Product not found")

    client_ip = request.client.host if request and request.client else "unknown"

    try:
        stripe_client = get_stripe_client()
        base_url = get_base_url()
        success_url = f"{base_url}/thank-you?session_id={{CHECKOUT_SESSION_ID}}&product={product_type}"
        cancel_path = (
            "pdpa"
            if "pdpa" in (product_type or "")
            else (
                "notarization"
                if "compliance_notarization" in (product_type or "")
                else (
                    "supply-chain"
                    if "supply_chain" in (product_type or "")
                    else (
                        "rfp-acceleration"
                        if "rfp_" in (product_type or "")
                        else (
                            "vendor-proof"
                            if (product_type or "") == "vendor_proof"
                            else "compliance"
                        )
                    )
                )
            )
        )
        cancel_url = f"{base_url}/{cancel_path}"

        _mode = MODE_MAP.get(product_type, "payment") if product_type else "payment"
        _session_kwargs2: dict = dict(
            mode=_mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"product_type": product_type or "", "client_ip": client_ip},
            allow_promotion_codes=True,
        )
        if _mode != "payment":
            _session_kwargs2["payment_method_types"] = ["card"]
        session = stripe_client.checkout.Session.create(**_session_kwargs2)

        return RedirectResponse(url=session.url)
    except RuntimeError as e:
        logger.error(f"Stripe configuration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create Stripe session (GET)")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/checkout/verify")
async def checkout_verify(session_id: str | None = None):
    """Verify a Stripe Checkout session by `session_id` and return payment status."""
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")

    try:
        stripe_client = get_stripe_client()
        # retrieve the session and expand the payment_intent for status details
        session = stripe_client.checkout.Session.retrieve(
            session_id, expand=["payment_intent"]
        )

        payment_status = (
            session.get("payment_status")
            if hasattr(session, "get")
            else getattr(session, "payment_status", None)
        )
        payment_intent = (
            session.get("payment_intent")
            if hasattr(session, "get")
            else getattr(session, "payment_intent", None)
        )
        metadata = (
            session.get("metadata")
            if hasattr(session, "get")
            else getattr(session, "metadata", None)
        )
        customer_email = None
        if hasattr(session, "get"):
            cust = session.get("customer_details")
            if cust:
                customer_email = cust.get("email")
        else:
            cust = getattr(session, "customer_details", None)
            if cust:
                customer_email = getattr(cust, "email", None)

        succeeded = False
        if payment_status == "paid":
            succeeded = True
        elif payment_intent and (
            (
                isinstance(payment_intent, dict)
                and payment_intent.get("status") == "succeeded"
            )
            or getattr(payment_intent, "status", None) == "succeeded"
        ):
            succeeded = True

        # Robust metadata read — Stripe SDK returns metadata as a `StripeObject`
        # (dict-like but NOT `isinstance(dict)`), so a plain isinstance gate
        # silently dropped product_type and broke the brief CTA. Try both
        # access patterns so we work whether the SDK returns dict or StripeObject.
        def _meta_get(key: str) -> str | None:
            if metadata is None:
                return None
            try:
                if hasattr(metadata, "get"):
                    v = metadata.get(key)
                    if v is not None:
                        return v
            except Exception:
                pass
            try:
                v = getattr(metadata, key, None)
                if v is not None:
                    return v
            except Exception:
                pass
            return None

        product_type_resolved = _meta_get("product_type")

        # RFP-bearing purchases defer kit generation until the buyer submits a
        # brief. The post-checkout page needs to know whether to show the
        # "Complete your brief" CTA — surface the pending intake on the verify
        # response so the frontend doesn't race the webhook with a separate call.
        requires_brief = product_type_resolved in {
            "rfp_complete", "rfp_express",
            "rfp_accelerator", "enterprise_bid_kit", "compliance_evidence_pack",
        }
        pending_rfp_intake_id = None
        # Compliance Evidence Pack also defers the BCEP 7-document intake. Surface
        # it so the success page can prompt the buyer to start the pack they paid
        # for (not just the RFP brief). Always defined for the metadata-stripped path.
        pending_evidence_pack_intake_id = None
        # `brief_satisfied` starts True for non-RFP products; gets cleared if
        # we discover a pending intake row for this session below. The intake
        # lookup runs unconditionally so a broken/missing product_type in
        # Stripe metadata cannot accidentally hide the brief CTA.
        brief_satisfied = not requires_brief

        if succeeded:
            # Cache hit → kit generation is in flight or done. This only
            # happens after the buyer submitted the brief.
            try:
                from app.core.cache import cache as _cache
                if _cache.get(_cache.cache_key(f"rfp_result:{session_id}")):
                    brief_satisfied = True
            except Exception as e:
                logger.warning("[checkout/verify] rfp_result cache probe failed: %s", e)

            # Always look up PendingRfpIntake by session_id. If we find a
            # pending row, surface it regardless of what product_type metadata
            # said — the row's existence is positive proof the webhook flagged
            # this purchase for an intake step. Same shape works for both the
            # metadata-intact path AND the metadata-stripped fallback.
            if customer_email:
                try:
                    from app.core.db import SessionLocal
                    from app.core.models import User as _U
                    from app.core.models import PendingRfpIntake
                    _db = SessionLocal()
                    from app.core.repositories.user_repository import UserRepository
                    try:
                        _user = UserRepository.get_by_email(_db, customer_email)
                        if _user:
                            # Compliance Evidence Pack: surface the outstanding BCEP
                            # intake for THIS session (session-scoped only, never
                            # "latest regardless of status" — a prior cycle's row
                            # would mislead the success page).
                            try:
                                from app.core.models import EvidencePack
                                _ep = (
                                    _db.query(EvidencePack)
                                    .filter(
                                        EvidencePack.user_id == _user.id,
                                        EvidencePack.session_id == session_id,
                                        EvidencePack.status == "intake_pending",
                                    )
                                    .order_by(EvidencePack.created_at.desc())
                                    .first()
                                )
                                if _ep:
                                    pending_evidence_pack_intake_id = str(_ep.id)
                            except Exception as _ee:
                                logger.warning("[checkout/verify] EvidencePack lookup failed: %s", _ee)

                            # Priority 1: a row tied to THIS session — authoritative
                            # for the current purchase, never overridden by prior cycles.
                            session_intake = (
                                _db.query(PendingRfpIntake)
                                .filter(
                                    PendingRfpIntake.user_id == _user.id,
                                    PendingRfpIntake.session_id == session_id,
                                )
                                .order_by(PendingRfpIntake.created_at.desc())
                                .first()
                            )
                            if session_intake and session_intake.status in ("pending", "needs_more_info"):
                                # Brief still owed for THIS purchase — show CTA.
                                # "needs_more_info" = a generated kit was blocked at
                                # the placeholder gate and the buyer must complete the
                                # missing facts before we regenerate.
                                pending_rfp_intake_id = str(session_intake.id)
                                brief_satisfied = False
                                # Backfill requires_brief so frontend logic is consistent
                                # when Stripe stripped product_type from metadata.
                                requires_brief = True
                            elif session_intake and session_intake.status == "submitted":
                                # Brief already filed for THIS session.
                                brief_satisfied = True
                                requires_brief = True
                            elif requires_brief and product_type_resolved in {"rfp_complete", "rfp_express"}:
                                # No session row yet — webhook race or failure.
                                # Lazy-create so the buyer has a brief link to follow.
                                resolved_url = _meta_get("vendor_url") or (getattr(_user, "website", "") or "") or None
                                resolved_company = _meta_get("company_name") or (getattr(_user, "company", "") or "") or None
                                _new = PendingRfpIntake(
                                    user_id=_user.id,
                                    session_id=session_id,
                                    rfp_product_type=product_type_resolved,
                                    bundle_source=product_type_resolved,
                                    vendor_url=resolved_url,
                                    company_name=resolved_company,
                                    status="pending",
                                )
                                _db.add(_new)
                                _db.flush()
                                pending_rfp_intake_id = str(_new.id)
                                brief_satisfied = False
                                _db.commit()
                                logger.warning(
                                    "[checkout/verify] Recovered missing PendingRfpIntake for %s session=%s id=%s",
                                    customer_email, session_id, pending_rfp_intake_id,
                                )
                            logger.info(
                                "[checkout/verify] resolved email=%s session=%s product=%s requires_brief=%s pending_id=%s brief_satisfied=%s",
                                customer_email, session_id, product_type_resolved,
                                requires_brief, pending_rfp_intake_id, brief_satisfied,
                            )
                        else:
                            logger.warning(
                                "[checkout/verify] No User row for customer_email=%s session=%s — verify will not find intake",
                                customer_email, session_id,
                            )
                    finally:
                        _db.close()
                except Exception as e:
                    logger.warning("[checkout/verify] PendingRfpIntake lookup/recover failed: %s", e)

        return JSONResponse(
            {
                "success": succeeded,
                "payment_status": payment_status,
                "session_id": (
                    session.get("id")
                    if hasattr(session, "get")
                    else getattr(session, "id", None)
                ),
                "product_type": product_type_resolved,
                "report_id": _meta_get("report_id") or (
                    session.get("client_reference_id")
                    if hasattr(session, "get")
                    else getattr(session, "client_reference_id", None)
                ),
                "customer_email": customer_email,
                "requires_brief": requires_brief,
                "brief_satisfied": brief_satisfied,
                "pending_rfp_intake_id": pending_rfp_intake_id,
                "pending_evidence_pack_intake_id": pending_evidence_pack_intake_id,
            },
            # The pending-intake state flips as the webhook fires. Any
            # intermediate cache would serve a stale "no intake" response
            # past that race and the result page would sit on "Generating".
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    except stripe.error.InvalidRequestError as e:
        logger.exception("Stripe API error during session retrieve")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to verify Stripe session")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rfp/result")
async def rfp_result(session_id: str | None = None):
    """Public endpoint: poll for RFP Kit download URL after payment.
    Returns 202 while generation is in progress, 200 with download_url when ready.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")

    from app.core.cache import cache as cache_mod

    data = cache_mod.get(cache_mod.cache_key(f"rfp_result:{session_id}"))
    if not data:
        return JSONResponse(
            status_code=202,
            content={"detail": "Not ready"},
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    return JSONResponse(data, headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/portal")
async def create_portal_session(
    token: str = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Create a Stripe Billing Portal session for the current user."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    from app.core.models import User

    from app.core.repositories.user_repository import UserRepository
    user = UserRepository.get_by_email(db, payload.get("sub"))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="No billing record found. Please purchase a product first.",
        )

    try:
        stripe_client = get_stripe_client()
        base_url = get_base_url()
        session = stripe_client.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{base_url}/pricing",
        )
        return RedirectResponse(url=session.url)
    except Exception as e:
        logger.exception("Failed to create billing portal session")
        raise HTTPException(status_code=500, detail=str(e))
