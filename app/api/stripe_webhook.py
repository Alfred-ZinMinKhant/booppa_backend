from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service
from app.services.email_service import EmailService
from app.billing.enforcement import enforce_tier
from datetime import datetime
import stripe
import logging
import json

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhook")
async def stripe_webhook(request: Request):
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

    # Handle the checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata") or {}

        # Try multiple metadata keys for report id
        report_id = (
            metadata.get("report_id")
            or metadata.get("reportId")
            or session.get("client_reference_id")
        )
        customer_email = None
        try:
            customer_email = session.get("customer_details", {}).get(
                "email"
            ) or session.get("customer_email")
        except Exception:
            customer_email = session.get("customer_email")

        if not report_id:
            logger.warning(
                "Checkout session completed but no report_id found in metadata"
            )
            return {"received": True}

        db = SessionLocal()
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if not report:
                logger.error(
                    f"Report {report_id} not found for Stripe session {session.get('id')}"
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
            product_type = metadata.get("product_type")
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
            db.commit()

            # Trigger async processing for fulfillment (AI generation, PDF, Email)
            try:
                from app.workers.tasks import process_report_task
                process_report_task.delay(str(report.id))
                logger.info(f"Queued background processing for paid report {report_id}")
            except Exception as e:
                logger.error(f"Failed to queue background task for {report_id}: {e}")
                # Fallback: try to run it via background tasks if celery fails? 
                # For now just log, as celery is the main worker.

        finally:
            db.close()

    return {"received": True}
