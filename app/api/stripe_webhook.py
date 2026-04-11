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
from app.core.models_v10 import Referral
from datetime import datetime
import stripe
import logging
import json
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

router = APIRouter()


RFP_PRODUCT_TYPES = {"rfp_express", "rfp_complete"}
NOTARIZATION_PRODUCT_TYPES = {
    "compliance_notarization_1",
    "compliance_notarization_10",
    "compliance_notarization_50",
    # Supply chain packages use the same blockchain anchoring fulfillment
    "supply_chain_1",
    "supply_chain_10",
    "supply_chain_50",
}


async def _fulfill_notarization(report_id: str, customer_email: str | None) -> None:
    """
    Lightweight notarization fulfillment:
    1. Anchor the original file SHA-256 to the blockchain
    2. Generate a proper notarization certificate PDF
    3. Upload to S3, set pipeline flags, send email
    """
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[Notarize] Report {report_id} not found")
            return

        assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        file_hash = assessment.get("file_hash") or report.audit_hash
        original_filename = assessment.get("original_filename", "document")
        file_size = assessment.get("file_size_bytes")
        contact_email = customer_email or assessment.get("contact_email") or assessment.get("customer_email")

        # Step 1: Anchor file hash on blockchain
        tx_hash = None
        try:
            blockchain = BlockchainService()
            tx_hash = await blockchain.anchor_evidence(
                file_hash, metadata=f"notarization:{report_id}"
            )
            # Only store a real hex tx_hash; None means already anchored (no new tx)
            if tx_hash:
                report.tx_hash = tx_hash
            report.audit_hash = file_hash  # keep as original file hash for verification
            assessment["blockchain_anchored"] = True
            assessment["blockchain_anchored_at"] = datetime.utcnow().isoformat()
            report.assessment_data = assessment
            flag_modified(report, "assessment_data")
            db.commit()
            logger.info(f"[Notarize] Anchored {file_hash[:16]}… tx={tx_hash}")
        except Exception as e:
            logger.error(f"[Notarize] Blockchain anchor failed for {report_id}: {e}")

        # Step 2: Build verify URL
        verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{file_hash}"
        polygonscan_url = (
            f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{tx_hash}" if tx_hash else None
        )

        # Step 3: Generate notarization certificate PDF
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            pdf_data = {
                "report_id": report_id,
                "framework": "compliance_notarization",
                "company_name": report.company_name,
                "created_at": report.created_at.isoformat() if report.created_at else datetime.utcnow().isoformat(),
                "status": "completed",
                "tx_hash": tx_hash,
                "audit_hash": file_hash,
                "original_filename": original_filename,
                "file_size": file_size,
                "verify_url": verify_url,
                "polygonscan_url": polygonscan_url,
                "proof_header": "BOOPPA-PROOF-SG",
                "schema_version": "1.0",
                "network": "Polygon Amoy Testnet",
                "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
                "payment_confirmed": True,
                "tier": "pro",
                "contact_email": contact_email,
                "base_url": "https://www.booppa.io",
            }
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
            assessment["pdf_generated"] = True
            assessment["pdf_generated_at"] = datetime.utcnow().isoformat()
            report.assessment_data = assessment
            flag_modified(report, "assessment_data")
            db.commit()
        except Exception as e:
            logger.error(f"[Notarize] PDF generation failed for {report_id}: {e}")

        # Step 4: Upload PDF to S3
        pdf_url = None
        if pdf_bytes:
            try:
                storage = S3Service()
                pdf_url = await storage.upload_pdf(pdf_bytes, report_id)
                report.s3_url = pdf_url
                assessment["s3_uploaded"] = True
                assessment["s3_uploaded_at"] = datetime.utcnow().isoformat()
                assessment["verify_url"] = verify_url
                assessment["polygonscan_url"] = polygonscan_url
                report.assessment_data = assessment
                flag_modified(report, "assessment_data")
                db.commit()
            except Exception as e:
                logger.error(f"[Notarize] S3 upload failed for {report_id}: {e}")

        # Step 5: Mark completed
        report.status = "completed"
        report.completed_at = datetime.utcnow()
        db.commit()

        # Step 6: Send email
        if contact_email:
            try:
                email_svc = EmailService()
                download_section = (
                    f'<p><a href="{pdf_url}" style="background-color:#10b981;color:#fff;'
                    f'padding:10px 24px;text-decoration:none;border-radius:6px;font-weight:bold;">'
                    f'Download Notarization Certificate (PDF)</a></p>'
                    if pdf_url
                    else "<p>Your certificate will be available on the BOOPPA website once processing is complete.</p>"
                )
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;">
                  <h2 style="color:#10b981;">Your Notarization Certificate is Ready</h2>
                  <p>Hello {report.company_name or "Customer"},</p>
                  <p>Your blockchain notarization certificate for
                     <strong>{original_filename}</strong> has been generated.</p>
                  <p><strong>SHA-256 Hash:</strong> <code>{file_hash}</code></p>
                  {'<p><strong>Blockchain TX:</strong> <a href="' + polygonscan_url + '">' + (tx_hash or '') + '</a></p>' if polygonscan_url else ''}
                  {download_section}
                  <p style="color:#64748b;font-size:12px;">
                    Certificate ID: {report_id}<br>
                    Network: Polygon Amoy Testnet
                  </p>
                  <p>Thank you for using BOOPPA.</p>
                </body></html>
                """
                await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Notarization Certificate is Ready — {original_filename}",
                    body_html=body_html,
                )
            except Exception as e:
                logger.error(f"[Notarize] Email failed for {report_id}: {e}")

        logger.info(f"[Notarize] Fulfilled {report_id}: tx={tx_hash} pdf={pdf_url}")
    except Exception as e:
        logger.error(f"[Notarize] Fulfillment error for {report_id}: {e}")
    finally:
        db.close()


async def _fulfill_rfp_package(
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
    session_id: str | None = None,
    intake_data: dict | None = None,
) -> None:
    """Background task: generate and deliver the RFP Kit package after payment."""
    db = SessionLocal()
    try:
        from app.services.rfp_express_builder import RFPExpressBuilder
        rfp_details = {"description": rfp_description} if rfp_description else None
        if intake_data:
            rfp_details = {**(rfp_details or {}), "intake": intake_data}
        builder = RFPExpressBuilder(vendor_id=vendor_id, vendor_email=vendor_email, session_id=session_id)
        result = await builder.generate_express_package(
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_details=rfp_details,
            db=db,
            product_type=product_type,
        )
        download_url = result.get("download_url")
        logger.info(
            f"RFP package fulfilled: product={product_type} vendor={vendor_id} "
            f"url={download_url} errors={result.get('errors')}"
        )
        # Store result keyed by session_id so the result page can retrieve it
        if session_id and download_url:
            from app.core.cache import cache as cache_mod
            cache_mod.set(
                cache_mod.cache_key(f"rfp_result:{session_id}"),
                {
                    "download_url": download_url,
                    "docx_url": result.get("docx_url"),
                    "product_type": product_type,
                    "company_name": company_name,
                    "vendor_url": vendor_url,
                    "qa_answers": result.get("qa_answers", []),
                    "tx_hash": result.get("tx_hash"),
                    "polygonscan_url": result.get("polygonscan_url"),
                    "generated_at": result.get("generated_at"),
                    "expires_at": result.get("expires_at"),
                    "data_sources": result.get("data_sources", {}),
                    "discrepancies": result.get("discrepancies", []),
                    "warnings": result.get("warnings", []),
                    "answer_source": result.get("answer_source", "ai_grounded"),
                },
                ttl=604800,  # 7 days
            )
    except Exception as e:
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {e}")
    finally:
        db.close()


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
        )

        if not report_id:
            # RFP products are self-contained — no pre-existing Report record required
            if product_type in RFP_PRODUCT_TYPES:
                vendor_url   = metadata.get("vendor_url", "")
                company_name = metadata.get("company_name", "")
                if vendor_url and company_name:
                    vendor_id = metadata.get("vendor_id") or customer_email or "anonymous"
                    session_id = session.get("id")
                    intake_dict = None
                    if metadata.get("has_intake") == "1" and session_id:
                        from app.core.cache import cache as cache_mod
                        cached_intake = cache_mod.get(cache_mod.cache_key(f"rfp_intake:{session_id}"))
                        if isinstance(cached_intake, dict):
                            intake_dict = cached_intake
                    from app.workers.tasks import fulfill_rfp_task
                    fulfill_rfp_task.delay(
                        product_type=product_type,
                        vendor_id=vendor_id,
                        vendor_email=customer_email or "",
                        vendor_url=vendor_url,
                        company_name=company_name,
                        rfp_description=metadata.get("rfp_description"),
                        session_id=session.get("id"),
                        intake_data=intake_dict,
                    )
                    logger.info(
                        f"Queued RFP {product_type} fulfillment (no report_id) "
                        f"vendor={vendor_id} url={vendor_url}"
                    )
                else:
                    logger.error(
                        f"RFP fulfillment skipped: missing vendor_url or company_name "
                        f"in metadata={metadata}"
                    )
                return {"received": True}

            logger.warning(
                "Checkout session completed but no report_id found in metadata"
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

            # Notarization — lightweight anchor + certificate (no AI, no website scan)
            if product_type in NOTARIZATION_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_notarization_task
                fulfill_notarization_task.delay(str(report.id), customer_email)
                logger.info(f"Queued notarization fulfillment for report {report_id}")

            # RFP Express / Complete — generate PDF + email immediately
            elif product_type in RFP_PRODUCT_TYPES:
                vendor_id   = metadata.get("vendor_id") or str(report.owner_id)
                vendor_url  = metadata.get("vendor_url") or metadata.get("website_url", "")
                company_name = metadata.get("company_name") or metadata.get("company", "")
                rfp_desc    = metadata.get("rfp_description")
                if not vendor_url or not company_name:
                    logger.error(
                        f"RFP fulfillment missing vendor_url or company_name for report {report_id}; "
                        f"metadata={metadata}"
                    )
                else:
                    from app.workers.tasks import fulfill_rfp_task
                    session_id = session.get("id")
                    intake_dict = None
                    if metadata.get("has_intake") == "1" and session_id:
                        from app.core.cache import cache as cache_mod
                        cached_intake = cache_mod.get(cache_mod.cache_key(f"rfp_intake:{session_id}"))
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

    # ── Post-checkout: upgrade user plan + close referral reward loop ────────
    if event["type"] == "checkout.session.completed":
        raw = json.loads(payload) if isinstance(payload, (str, bytes)) else {}
        session = raw.get("data", {}).get("object", {}) if raw else {}
        customer_email = (
            (session.get("customer_details") or {}).get("email")
            or session.get("customer_email")
        )
        if customer_email:
            _db = SessionLocal()
            try:
                user = _db.query(User).filter(User.email == customer_email).first()
                if user and getattr(user, "plan", "free") != "enterprise":
                    metadata = session.get("metadata") or {}
                    product = metadata.get("product_type") or ""
                    new_plan = "enterprise" if "enterprise" in product else "pro"
                    user.plan = new_plan
                    # Close the referral reward loop: find a SIGNED_UP referral for this
                    # user and mark reward_claimed so the referrer gets credit.
                    referral = (
                        _db.query(Referral)
                        .filter(
                            Referral.referred_id == user.id,
                            Referral.status == "SIGNED_UP",
                            Referral.reward_claimed == False,
                        )
                        .first()
                    )
                    if referral:
                        referral.status = "REWARDED"
                        referral.reward_claimed = True
                        referral.reward_claimed_at = datetime.utcnow()
                    _db.commit()
                    logger.info(
                        f"[Webhook] Upgraded user {customer_email} to plan={new_plan}"
                        + (f"; referral {referral.referral_code} rewarded" if referral else "")
                    )
            except Exception as exc:
                logger.error(f"[Webhook] Plan upgrade failed for {customer_email}: {exc}")
            finally:
                _db.close()

    return {"received": True}
