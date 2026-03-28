from fastapi import APIRouter, BackgroundTasks, Request, HTTPException
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
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

router = APIRouter()


RFP_PRODUCT_TYPES = {"rfp_express", "rfp_complete"}


async def _fulfill_rfp_package(
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
) -> None:
    """Background task: generate and deliver the RFP Kit package after payment."""
    db = SessionLocal()
    try:
        from app.services.rfp_express_builder import RFPExpressBuilder
        rfp_details = {"description": rfp_description} if rfp_description else None
        builder = RFPExpressBuilder(vendor_id=vendor_id, vendor_email=vendor_email)
        result = await builder.generate_express_package(
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_details=rfp_details,
            db=db,
        )
        logger.info(
            f"RFP package fulfilled: product={product_type} vendor={vendor_id} "
            f"url={result.get('download_url')} errors={result.get('errors')}"
        )
    except Exception as e:
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {e}")
    finally:
        db.close()


@router.post("/webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
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
            flag_modified(report, "assessment_data")
            db.commit()

            # RFP Express / Complete — generate PDF + email immediately
            if product_type in RFP_PRODUCT_TYPES:
                vendor_id   = metadata.get("vendor_id") or str(report.user_id)
                vendor_url  = metadata.get("vendor_url") or metadata.get("website_url", "")
                company_name = metadata.get("company_name") or metadata.get("company", "")
                rfp_desc    = metadata.get("rfp_description")
                if not vendor_url or not company_name:
                    logger.error(
                        f"RFP fulfillment missing vendor_url or company_name for report {report_id}; "
                        f"metadata={metadata}"
                    )
                else:
                    background_tasks.add_task(
                        _fulfill_rfp_package,
                        product_type=product_type,
                        vendor_id=vendor_id,
                        vendor_email=customer_email or "",
                        vendor_url=vendor_url,
                        company_name=company_name,
                        rfp_description=rfp_desc,
                    )
                    logger.info(
                        f"Queued RFP {product_type} fulfillment for vendor {vendor_id}"
                    )
            else:
                # Standard report: trigger async processing via Celery
                try:
                    from app.workers.tasks import process_report_task
                    process_report_task.delay(str(report.id))
                    logger.info(f"Queued background processing for paid report {report_id}")
                except Exception as e:
                    logger.error(f"Failed to queue background task for {report_id}: {e}")

        finally:
            db.close()

    return {"received": True}
