from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
import os
import stripe
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

# Price map: reads environment variables. Missing values will cause 400 errors later.
PRICE_MAP = {
    "pdpa_quick_scan": os.environ.get("STRIPE_PDPA_QUICK_SCAN")
    or os.environ.get("NEXT_PUBLIC_STRIPE_PDPA_QUICK_SCAN"),
    "pdpa_basic": os.environ.get("STRIPE_PDPA_BASIC")
    or os.environ.get("NEXT_PUBLIC_STRIPE_PDPA_BASIC"),
    "pdpa_pro": os.environ.get("STRIPE_PDPA_PRO")
    or os.environ.get("NEXT_PUBLIC_STRIPE_PDPA_PRO"),
    "compliance_standard": os.environ.get("STRIPE_COMPLIANCE_STANDARD")
    or os.environ.get("NEXT_PUBLIC_STRIPE_COMPLIANCE_STANDARD"),
    "compliance_pro": os.environ.get("STRIPE_COMPLIANCE_PRO")
    or os.environ.get("NEXT_PUBLIC_STRIPE_COMPLIANCE_PRO"),
    "supply_chain_1": os.environ.get("STRIPE_SUPPLY_CHAIN_1")
    or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_1"),
    "supply_chain_10": os.environ.get("STRIPE_SUPPLY_CHAIN_10")
    or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_10"),
    "supply_chain_50": os.environ.get("STRIPE_SUPPLY_CHAIN_50")
    or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_50"),
    "compliance_notarization_1": (
        os.environ.get("STRIPE_COMPLIANCE_NOTARIZATION_1")
        or os.environ.get("NEXT_PUBLIC_STRIPE_COMPLIANCE_NOTARIZATION_1")
        or os.environ.get("STRIPE_SUPPLY_CHAIN_1")
        or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_1")
    ),
    "compliance_notarization_10": (
        os.environ.get("STRIPE_COMPLIANCE_NOTARIZATION_10")
        or os.environ.get("NEXT_PUBLIC_STRIPE_COMPLIANCE_NOTARIZATION_10")
        or os.environ.get("STRIPE_SUPPLY_CHAIN_10")
        or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_10")
    ),
    "compliance_notarization_50": (
        os.environ.get("STRIPE_COMPLIANCE_NOTARIZATION_50")
        or os.environ.get("NEXT_PUBLIC_STRIPE_COMPLIANCE_NOTARIZATION_50")
        or os.environ.get("STRIPE_SUPPLY_CHAIN_50")
        or os.environ.get("NEXT_PUBLIC_STRIPE_SUPPLY_CHAIN_50")
    ),
}

MODE_MAP = {
    "pdpa_quick_scan": "payment",
    "pdpa_basic": "subscription",
    "pdpa_pro": "subscription",
    "compliance_standard": "subscription",
    "compliance_pro": "subscription",
    "supply_chain_1": "payment",
    "supply_chain_10": "payment",
    "supply_chain_50": "payment",
    "compliance_notarization_1": "payment",
    "compliance_notarization_10": "payment",
    "compliance_notarization_50": "payment",
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


@router.post("/checkout")
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
        price_id = PRICE_MAP.get(product_type)

    if not price_id:
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
        # simple cancel URL mapping
        cancel_path = (
            "pdpa"
            if "pdpa" in (product_type or "")
            else (
                "compliance-notarization"
                if (
                    "supply_chain" in (product_type or "")
                    or "compliance_notarization" in (product_type or "")
                )
                else "compliance"
            )
        )
        cancel_url = f"{base_url}/{cancel_path}"

        metadata = {"product_type": product_type or "", "client_ip": client_ip}
        if report_id:
            metadata["report_id"] = str(report_id)
        # include customer email in metadata when provided
        if prefill_email:
            metadata["customer_email"] = prefill_email

        session = stripe_client.checkout.Session.create(
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            payment_method_types=["card"],
            metadata=metadata,
            client_reference_id=str(report_id) if report_id else None,
            customer_email=prefill_email if prefill_email else None,
            allow_promotion_codes=True,
        )
        logger.info(
            f"Created Stripe session id={getattr(session, 'id', None)} url={getattr(session, 'url', None)} metadata={metadata}"
        )

        return JSONResponse({"url": session.url})
    except RuntimeError as e:
        logger.error(f"Stripe configuration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create Stripe session")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/checkout")
async def checkout_get(
    product: str | None = None,
    prefill_email: str | None = None,
    request: Request = None,
):
    """Support GET requests like /checkout?product=... to create a session and redirect the user to Stripe."""
    product_type = product
    price_id = PRICE_MAP.get(product_type)

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
                "compliance-notarization"
                if (
                    "supply_chain" in (product_type or "")
                    or "compliance_notarization" in (product_type or "")
                )
                else "compliance"
            )
        )
        cancel_url = f"{base_url}/{cancel_path}"

        session = stripe_client.checkout.Session.create(
            mode=MODE_MAP.get(product_type, "payment"),
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            payment_method_types=["card"],
            metadata={"product_type": product_type or "", "client_ip": client_ip},
            allow_promotion_codes=True,
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
                "customer_email": customer_email,
            }
        )
    except stripe.error.InvalidRequestError as e:
        logger.exception("Stripe API error during session retrieve")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to verify Stripe session")
        raise HTTPException(status_code=500, detail=str(e))
