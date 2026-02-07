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

            report.assessment_data = ad
            db.commit()

            # Anchor evidence on blockchain
            blockchain = BlockchainService()
            evidence_hash = report.audit_hash
            if policy.get("features", {}).get("blockchain") and policy.get("paid"):
                try:
                    tx_hash = await blockchain.anchor_evidence(evidence_hash)
                    report.tx_hash = tx_hash
                    db.commit()
                except Exception as e:
                    logger.error(f"Anchoring failed for report {report_id}: {e}")
                    # proceed but leave tx_hash empty

            # Regenerate PDF with updated tx_hash/payment flag and structured report
            pdf_service = PDFService()
            verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/{evidence_hash}"

            if policy.get("features", {}).get("pdf") and policy.get("paid"):
                ad["verify_url"] = verify_url
                ad["proof_header"] = "BOOPPA-PROOF-SG"
                ad["schema_version"] = "1.0"
                report.assessment_data = ad
                db.commit()

            # Try to reuse existing structured Booppa report saved in assessment_data,
            # otherwise generate one now so the PDF includes full sections.
            structured_report = None
            try:
                if isinstance(
                    report.assessment_data, dict
                ) and report.assessment_data.get("booppa_report"):
                    structured_report = report.assessment_data.get("booppa_report")
                else:
                    booppa = BooppaAIService()
                    structured_report = await booppa.generate_compliance_report(
                        report.assessment_data or {}
                    )
            except Exception as e:
                logger.error(f"Failed to obtain structured report for {report_id}: {e}")

            pdf_data = {
                "report_id": str(report.id),
                "framework": report.framework,
                "company_name": report.company_name,
                "created_at": report.created_at.isoformat(),
                # Ensure regenerated PDF reflects completed status
                "status": "completed",
                "tx_hash": report.tx_hash,
                "audit_hash": report.audit_hash,
                "ai_narrative": report.ai_narrative or None,
                "structured_report": structured_report,
                "payment_confirmed": True,
                "tier": policy.get("tier"),
                "proof_header": ad.get("proof_header"),
                "schema_version": ad.get("schema_version"),
                "verify_url": ad.get("verify_url") or verify_url,
                "contact_email": (
                    ad.get("contact_email") if isinstance(ad, dict) else None
                ),
                "base_url": (
                    ad.get("base_url")
                    if isinstance(ad, dict) and ad.get("base_url")
                    else "https://www.booppa.io"
                ),
            }

            # Ensure screenshot is present for regenerated PDF (prefer stored value)
            try:
                if isinstance(
                    report.assessment_data, dict
                ) and report.assessment_data.get("site_screenshot"):
                    pdf_data["site_screenshot"] = report.assessment_data.get(
                        "site_screenshot"
                    )
                else:
                    url = None
                    if isinstance(report.assessment_data, dict):
                        url = (
                            report.assessment_data.get("url") or report.company_website
                        )
                    if url:
                        from app.services.screenshot_service import (
                            capture_screenshot_base64,
                        )

                        ss_b64 = await asyncio.to_thread(capture_screenshot_base64, url)
                        if ss_b64:
                            pdf_data["site_screenshot"] = ss_b64
            except Exception as e:
                logger.warning(
                    f"Failed to attach screenshot for regenerated PDF {report_id}: {e}"
                )

            pdf_bytes = None
            if policy.get("features", {}).get("pdf") and policy.get("paid"):
                try:
                    pdf_bytes = pdf_service.generate_pdf(pdf_data)
                except Exception as e:
                    logger.error(
                        f"PDF regeneration failed for report {report_id}: {e}"
                    )

            # Upload PDF to S3
            if pdf_bytes:
                storage = S3Service()
                try:
                    pdf_url = await storage.upload_pdf(pdf_bytes, str(report.id))
                    report.s3_url = pdf_url
                    report.file_key = f"reports/{report.id}.pdf"
                    # Mark report completed when PDF is uploaded
                    report.status = "completed"
                    report.completed_at = datetime.utcnow()
                    db.commit()
                except Exception as e:
                    logger.error(
                        f"Failed to upload regenerated PDF for report {report_id}: {e}"
                    )

            # Send notification email
            email_to = ad.get("contact_email") or customer_email or "user@example.com"
            email_service = EmailService()
            try:
                await email_service.send_report_ready_email(
                    to_email=email_to,
                    report_url=report.s3_url or "",
                    user_name=email_to.split("@")[0],
                    report_id=str(report.id),
                )
            except Exception as e:
                logger.error(
                    f"Failed to send report ready email for report {report_id}: {e}"
                )

        finally:
            db.close()

    return {"received": True}
