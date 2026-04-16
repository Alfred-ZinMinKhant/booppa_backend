from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
import os
import hashlib
import stripe
import logging

logger = logging.getLogger(__name__)
router = APIRouter()
_limiter = Limiter(key_func=get_remote_address)

# Price map: resolved at request time so env vars set after import are picked up.
def _get_price(product_type: str) -> str | None:
    """Look up the Stripe Price ID for a product at call time (not import time)."""
    env_key = product_type.upper()
    return (
        os.environ.get(f"STRIPE_{env_key}")
        or os.environ.get(f"NEXT_PUBLIC_STRIPE_{env_key}")
    )

MODE_MAP = {
    # One-time products
    "pdpa_quick_scan": "payment",
    "vendor_proof": "payment",
    "rfp_express": "payment",
    "rfp_complete": "payment",
    "compliance_notarization_1": "payment",
    "compliance_notarization_10": "payment",
    "compliance_notarization_50": "payment",
    "supply_chain_1": "payment",
    "supply_chain_10": "payment",
    "supply_chain_50": "payment",
    # Bundles (one-time)
    "vendor_trust_pack": "payment",
    "rfp_accelerator": "payment",
    "enterprise_bid_kit": "payment",
    # Subscriptions
    "vendor_active_monthly": "subscription",
    "vendor_active_annual": "subscription",
    "pdpa_monitor_monthly": "subscription",
    "pdpa_monitor_annual": "subscription",
    "enterprise_monthly": "subscription",
    "enterprise_pro_monthly": "subscription",
    # Legacy subscription keys
    "pdpa_basic": "subscription",
    "pdpa_pro": "subscription",
    "compliance_standard": "subscription",
    "compliance_pro": "subscription",
}


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


def _checkout_idempotency_key(product_type: str, price_id: str, client_ip: str, report_id=None, email=None) -> str:
    # Include client_ip so anonymous checkouts (no report/email) don't collide across users.
    raw = f"{product_type}:{price_id}:{client_ip}:{report_id or ''}:{email or ''}"
    return "checkout-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


@router.post("/checkout")
@_limiter.limit("20/minute")
async def checkout_post(request: Request):
    """Create a Stripe Checkout session via POST with JSON body { productType, priceId (optional), prefill_email (optional) }"""
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
    )

    if not product_type and not price_id:
        raise HTTPException(status_code=400, detail="Missing productType or priceId")

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
            success_url = f"{base_url}/rfp-acceleration/result?session_id={{CHECKOUT_SESSION_ID}}"
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
        elif _pt in ("enterprise_monthly", "enterprise_pro_monthly"):
            cancel_path = "pricing"
        else:
            cancel_path = "pricing"
        cancel_url = f"{base_url}/{cancel_path}"

        vendor_url = data.get("vendor_url", "")
        company_name = data.get("company_name", "")
        rfp_description = data.get("rfp_description", "")
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
        if rfp_description:
            metadata["rfp_description"] = rfp_description
        # Buyer-supplied facts indicator
        if intake_data:
            metadata["has_intake"] = "1"

        # PayNow is only supported for one-time payments, not subscriptions
        payment_methods = ["card", "paynow"] if mode == "payment" else ["card"]
        _idem_key = _checkout_idempotency_key(product_type or "", price_id, client_ip, report_id, prefill_email)
        session = stripe_client.checkout.Session.create(
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            payment_method_types=payment_methods,
            metadata=metadata,
            client_reference_id=str(report_id) if report_id else None,
            customer_email=prefill_email if prefill_email else None,
            allow_promotion_codes=True,
            idempotency_key=_idem_key,
        )
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

        if intake_data and hasattr(session, "id"):
            from app.core.cache import cache as cache_mod
            cache_mod.set(
                cache_mod.cache_key(f"rfp_intake:{session.id}"),
                intake_data,
                ttl=86400  # 24 hours
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
        _payment_methods = ["card", "paynow"] if _mode == "payment" else ["card"]
        _idem_key = _checkout_idempotency_key(product_type or "", price_id, client_ip, email=prefill_email)
        session = stripe_client.checkout.Session.create(
            mode=_mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            payment_method_types=_payment_methods,
            metadata={"product_type": product_type or "", "client_ip": client_ip},
            allow_promotion_codes=True,
            idempotency_key=_idem_key,
        )

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

        return JSONResponse(
            {
                "success": succeeded,
                "payment_status": payment_status,
                "session_id": (
                    session.get("id")
                    if hasattr(session, "get")
                    else getattr(session, "id", None)
                ),
                "product_type": (
                    (metadata or {}).get("product_type")
                    if isinstance(metadata, dict)
                    else None
                ),
                "report_id": (
                    (metadata or {}).get("report_id")
                    if isinstance(metadata, dict)
                    else None
                ) or (
                    session.get("client_reference_id")
                    if hasattr(session, "get")
                    else getattr(session, "client_reference_id", None)
                ),
                "customer_email": customer_email,
            }
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
        return JSONResponse(status_code=202, content={"detail": "Not ready"}, headers={"Cache-Control": "no-store, max-age=0"})

    return JSONResponse(data, headers={"Cache-Control": "no-store, max-age=0"})

