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
    "supply_chain_1",
    "supply_chain_10",
    "supply_chain_50",
}
VENDOR_PROOF_PRODUCT_TYPES = {"vendor_proof"}
PDPA_PRODUCT_TYPES = {"pdpa_quick_scan", "pdpa_basic", "pdpa_pro", "pdpa_snapshot"}
SUBSCRIPTION_PRODUCT_TYPES = {
    "vendor_active_monthly", "vendor_active_annual",
    "pdpa_monitor_monthly", "pdpa_monitor_annual",
    "enterprise_monthly", "enterprise_pro_monthly",
}

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
        "notarization_count": 7,   # 2 from Trust Pack + 5 additional
        "rfp": "rfp_complete",
    },
}


async def _activate_subscription(
    product_type: str,
    customer_email: str | None,
    stripe_subscription_id: str | None,
    stripe_customer_id: str | None,
) -> None:
    """
    Persist subscription state when a new Stripe subscription is created or renewed.
    Grants the appropriate platform role/plan to the user.
    """
    db = SessionLocal()
    try:
        from app.core.models import User

        user = None
        if customer_email:
            user = db.query(User).filter(User.email == customer_email).first()

        if not user:
            logger.warning(f"[Subscription] No user found for email={customer_email}")
            return

        # Map product_type → platform plan
        plan_map = {
            "vendor_active_monthly": "vendor_active",
            "vendor_active_annual":  "vendor_active",
            "pdpa_monitor_monthly":  "pdpa_monitor",
            "pdpa_monitor_annual":   "pdpa_monitor",
            "enterprise_monthly":    "enterprise",
            "enterprise_pro_monthly":"enterprise_pro",
        }
        new_plan = plan_map.get(product_type, "pro")

        user.plan = new_plan
        if stripe_subscription_id:
            user.stripe_subscription_id = stripe_subscription_id
        if stripe_customer_id:
            user.stripe_customer_id = stripe_customer_id
        db.commit()
        logger.info(f"[Subscription] Activated plan={new_plan} for user={customer_email}")

        # Send confirmation email
        if customer_email:
            plan_labels = {
                "vendor_active": "Vendor Active",
                "pdpa_monitor":  "PDPA Monitor",
                "enterprise":    "Enterprise",
                "enterprise_pro":"Enterprise Pro",
            }
            label = plan_labels.get(new_plan, new_plan)
            try:
                email_svc = EmailService()
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:20px;">{label} — Activated</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>Your <strong>{label}</strong> subscription is now active.</p>
                    <p>
                      <a href="https://www.booppa.io/vendor/dashboard"
                         style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                                border-radius:8px;font-weight:bold;display:inline-block;">
                        Go to Dashboard →
                      </a>
                    </p>
                    <p style="color:#64748b;font-size:12px;margin-top:24px;">booppa.io</p>
                  </div>
                </body></html>
                """
                await email_svc.send_html_email(
                    to_email=customer_email,
                    subject=f"Your {label} subscription is active — BOOPPA",
                    body_html=body_html,
                )
            except Exception as e:
                logger.error(f"[Subscription] Email failed for {customer_email}: {e}")

    except Exception as e:
        logger.error(f"[Subscription] Activation error for {product_type}: {e}")
        db.rollback()
    finally:
        db.close()


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
        fulfill_notarization_task,
        fulfill_rfp_task,
    )

    db = SessionLocal()
    try:
        # Create a synthetic Report for components that need one (VP, PDPA)
        # If report_id was passed from checkout, use it; otherwise create stubs.
        from app.core.models import Report
        import uuid as _uuid

        base_report = db.query(Report).filter(Report.id == report_id).first() if report_id else None
        owner_id = base_report.owner_id if base_report else None

        # Resolve owner from customer_email when no base report exists (all bundle purchases)
        if not owner_id and customer_email:
            from app.core.models import User
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                owner_id = user.id
                logger.info(f"[Bundle:{product_type}] Resolved owner_id={owner_id} from email={customer_email}")
            else:
                logger.warning(f"[Bundle:{product_type}] No user found for email={customer_email} — stubs will have no owner")

        company_name = (base_report.company_name if base_report else None) or metadata.get("company_name", "")
        website = (base_report.company_website if base_report else None) or metadata.get("vendor_url", "")

        # Helper to create a stub report for a component
        def _make_stub(framework: str) -> str:
            stub = Report(
                owner_id=owner_id or _uuid.uuid4(),
                framework=framework,
                company_name=company_name,
                company_website=website,
                status="pending",
                assessment_data={
                    "payment_confirmed": True,
                    "on_page_only": False,
                    "tier": "pro",
                    "contact_email": customer_email,
                    "bundle_source": product_type,
                },
            )
            db.add(stub)
            db.flush()
            return str(stub.id)

        # 1. Vendor Proof
        if components.get("vendor_proof"):
            vp_id = str(base_report.id) if base_report and base_report.framework in ("vendor_proof",) else _make_stub("vendor_proof")
            db.commit()
            fulfill_vendor_proof_task.delay(vp_id, customer_email)
            logger.info(f"[Bundle:{product_type}] Queued vendor_proof for report {vp_id}")

        # 2. PDPA Snapshot
        if components.get("pdpa"):
            pdpa_id = _make_stub("pdpa_quick_scan")
            db.commit()
            fulfill_pdpa_task.delay(pdpa_id, customer_email)
            logger.info(f"[Bundle:{product_type}] Queued pdpa for report {pdpa_id}")

        # 3. Notarization credits (one task per document credit)
        notarization_count = components.get("notarization_count", 0)
        for i in range(notarization_count):
            notarization_id = _make_stub("compliance_notarization_1")
            db.commit()
            fulfill_notarization_task.delay(notarization_id, customer_email)
            logger.info(f"[Bundle:{product_type}] Queued notarization #{i+1} for report {notarization_id}")

        # 4. RFP component
        rfp_type = components.get("rfp")
        if rfp_type:
            vendor_url  = metadata.get("vendor_url", website)
            vendor_id   = str(owner_id) if owner_id else (customer_email or "anonymous")
            rfp_desc    = metadata.get("rfp_description")
            if vendor_url and company_name:
                fulfill_rfp_task.delay(
                    product_type=rfp_type,
                    vendor_id=vendor_id,
                    vendor_email=customer_email or "",
                    vendor_url=vendor_url,
                    company_name=company_name,
                    rfp_description=rfp_desc,
                    session_id=session_id,
                )
                logger.info(f"[Bundle:{product_type}] Queued {rfp_type} for vendor {vendor_id}")
                # Strategy 6 fires for rfp_accelerator (contains rfp_express)
                if rfp_type == "rfp_express":
                    sector = metadata.get("sector")
                    rfp_title = rfp_desc or "New procurement opportunity"
                    import asyncio as _asyncio
                    _asyncio.create_task(_fire_strategy_6(sector, rfp_title))
            else:
                logger.warning(f"[Bundle:{product_type}] RFP skipped — missing vendor_url or company_name")

    except Exception as e:
        logger.error(f"[Bundle] Fulfillment error for {product_type}: {e}")
        db.rollback()
    finally:
        db.close()


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


async def _fulfill_vendor_proof(report_id: str, customer_email: str | None) -> None:
    """
    Vendor Proof fulfillment:
    1. Create/upsert VerifyRecord (lifecycleStatus=ACTIVE, complianceScore=30)
    2. Create/upsert VendorStatusSnapshot (verificationDepth=BASIC, procurementReadiness=CONDITIONAL)
    3. Create/upsert VendorScore baseline (complianceScore=30, totalScore=30)
    4. Send confirmation email with embeddable badge HTML
    """
    db = SessionLocal()
    try:
        from app.core.models_v6 import VerifyRecord, LifecycleStatus, VerificationLevel, VendorScore
        from app.core.models_v8 import VendorStatusSnapshot

        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[VendorProof] Report {report_id} not found")
            return

        vendor_id = report.owner_id
        contact_email = customer_email or (report.assessment_data or {}).get("contact_email")

        # If the report was created via the public endpoint, owner_id is a random UUID.
        # Resolve the real user by email so VerifyRecord is linked to an actual account.
        if contact_email:
            from app.core.models import User
            real_user = db.query(User).filter(User.email == contact_email).first()
            if real_user:
                if str(vendor_id) != str(real_user.id):
                    logger.info(
                        f"[VendorProof] Remapping owner_id {vendor_id} → {real_user.id} "
                        f"via email={contact_email}"
                    )
                    vendor_id = real_user.id
                    report.owner_id = real_user.id
                    db.flush()

        if not vendor_id:
            logger.error(f"[VendorProof] No owner_id on report {report_id} and no user resolved from email")
            return
        company_name  = report.company_name or "Vendor"
        verify_url    = f"https://www.booppa.io/verify/{report_id}"

        # Step 1: Create or upsert VerifyRecord
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
        if verify:
            verify.lifecycle_status  = LifecycleStatus.ACTIVE
            verify.compliance_score  = max(verify.compliance_score or 0, 30)
            verify.verification_level = VerificationLevel.BASIC
            verify.last_refreshed_at = datetime.utcnow()
        else:
            verify = VerifyRecord(
                vendor_id=vendor_id,
                compliance_score=30,
                verification_level=VerificationLevel.BASIC,
                lifecycle_status=LifecycleStatus.ACTIVE,
                correlation_id=str(report_id),
            )
            db.add(verify)
        db.flush()

        # Step 2: Create or upsert VendorStatusSnapshot
        snapshot = db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id == vendor_id).first()
        if snapshot:
            if snapshot.verification_depth in ("UNVERIFIED", None):
                snapshot.verification_depth = "BASIC"
            if snapshot.procurement_readiness == "NOT_READY":
                snapshot.procurement_readiness = "CONDITIONAL"
            snapshot.confidence_score = max(snapshot.confidence_score or 0.0, 30.0)
            snapshot.computed_at = datetime.utcnow()
        else:
            snapshot = VendorStatusSnapshot(
                vendor_id=vendor_id,
                verification_depth="BASIC",
                monitoring_activity="ACTIVE",
                risk_signal="CLEAN",
                procurement_readiness="CONDITIONAL",
                confidence_score=30.0,
                evidence_count=0,
                notarization_depth=0,
                dual_silent_mode="SILENT_RISK_CAPTURE",
            )
            db.add(snapshot)

        # Step 3: Create or upsert VendorScore baseline
        score_row = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        if score_row:
            if (score_row.compliance_score or 0) < 30:
                score_row.compliance_score = 30
            if (score_row.total_score or 0) < 30:
                score_row.total_score = 30
            score_row.updated_at = datetime.utcnow()
        else:
            score_row = VendorScore(
                vendor_id=vendor_id,
                compliance_score=30,
                total_score=30,
            )
            db.add(score_row)

        # Mark report complete
        ad = report.assessment_data or {}
        ad["vendor_proof_fulfilled"] = True
        ad["verify_url"] = verify_url
        report.assessment_data = ad
        flag_modified(report, "assessment_data")
        report.status = "completed"
        report.completed_at = datetime.utcnow()
        db.commit()

        logger.info(f"[VendorProof] VerifyRecord + snapshot created for vendor {vendor_id}")

        # Step 4: Email with embeddable badge
        if contact_email:
            badge_html = (
                f'<a href="{verify_url}" target="_blank" rel="noopener noreferrer" '
                f'style="display:inline-flex;align-items:center;gap:8px;background:#0f172a;'
                f'color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;'
                f'font-family:Arial,sans-serif;font-size:13px;font-weight:600;">'
                f'<span style="color:#10b981;">✓</span> Verified on BOOPPA</a>'
            )
            body_html = f"""
            <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
              <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                <h1 style="color:#10b981;margin:0;font-size:20px;">Vendor Proof Activated</h1>
              </div>
              <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                <p>Hello <strong>{company_name}</strong>,</p>
                <p>Your Vendor Proof is now <strong style="color:#10b981;">active</strong>. You are now visible to procurement officers who filter by verified vendors on the BOOPPA platform.</p>
                <h3 style="color:#0f172a;">What changed on your profile</h3>
                <ul>
                  <li>Verification status: <strong>BASIC (Active)</strong></li>
                  <li>Compliance score baseline: <strong>30/100</strong></li>
                  <li>Procurement readiness: <strong>Conditional</strong></li>
                  <li>CAL Level 1 activated — personalised upgrade recommendations will appear in your dashboard</li>
                </ul>
                <h3 style="color:#0f172a;">Embed your Booppa Verified badge</h3>
                <p>Add this to your website or RFP proposals:</p>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-family:monospace;font-size:12px;word-break:break-all;">
                  {badge_html.replace('<', '&lt;').replace('>', '&gt;')}
                </div>
                <div style="margin-top:16px;">{badge_html}</div>
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/vendor/dashboard" style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
                    Go to Dashboard →
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;margin-top:24px;">
                  Verification ID: {report_id}<br>
                  Verified on: {datetime.utcnow().strftime('%d %B %Y')}<br>
                  booppa.io
                </p>
              </div>
            </body></html>
            """
            try:
                email_svc = EmailService()
                await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Vendor Proof is Active — {company_name}",
                    body_html=body_html,
                )
            except Exception as e:
                logger.error(f"[VendorProof] Email failed for {contact_email}: {e}")

        logger.info(f"[VendorProof] Fulfilled {report_id} for vendor {vendor_id}")
    except Exception as e:
        logger.error(f"[VendorProof] Fulfillment error for {report_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def _fulfill_pdpa(report_id: str, customer_email: str | None) -> None:
    """
    PDPA Snapshot fulfillment:
    1. Run the full on-page + AI scan (if not already done)
    2. Generate branded PDF report
    3. Upload to S3
    4. Update vendor compliance score (+8 to +25 pts)
    5. Write CertificateLog entry
    6. Send email with PDF download link
    """
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[PDPA] Report {report_id} not found")
            return

        assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        contact_email = customer_email or assessment.get("contact_email") or assessment.get("customer_email")
        company_name  = report.company_name or "Customer"
        website_url   = report.company_website or assessment.get("website", "")

        # ── Step 1: Ensure scan is complete ────────────────────────────────
        # If the report already has a risk_score from a prior scan, use it.
        # Otherwise trigger the generic processing task synchronously.
        risk_score = assessment.get("risk_score") or (
            assessment.get("risk_assessment", {}).get("score") if isinstance(assessment.get("risk_assessment"), dict) else None
        )
        if risk_score is None:
            # Scan not yet run — queue generic processing; it will generate PDF too
            try:
                from app.workers.tasks import process_report_task
                process_report_task.delay(str(report.id))
                logger.info(f"[PDPA] Queued generic scan for {report_id} (risk_score missing)")
            except Exception as e:
                logger.error(f"[PDPA] Could not queue scan for {report_id}: {e}")
            # Compliance score and CertificateLog will be written when scan completes
            return

        # ── Step 2: Generate PDF ────────────────────────────────────────────
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            pdf_data = {
                "report_id":     report_id,
                "framework":     report.framework or "pdpa_quick_scan",
                "company_name":  company_name,
                "company_url":   website_url,
                "created_at":    report.created_at.isoformat() if report.created_at else datetime.utcnow().isoformat(),
                "status":        "completed",
                "risk_score":    risk_score,
                "risk_level":    assessment.get("risk_level") or assessment.get("risk_assessment", {}).get("level", "MEDIUM"),
                "findings":      assessment.get("findings") or assessment.get("detailed_findings", []),
                "summary":       assessment.get("executive_summary", ""),
                "payment_confirmed": True,
                "tier":          assessment.get("tier", "pro"),
                "contact_email": contact_email,
                "base_url":      "https://www.booppa.io",
            }
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
        except Exception as e:
            logger.error(f"[PDPA] PDF generation failed for {report_id}: {e}")

        # ── Step 3: Upload PDF to S3 ────────────────────────────────────────
        pdf_url = None
        if pdf_bytes:
            try:
                storage = S3Service()
                pdf_url = await storage.upload_pdf(pdf_bytes, report_id)
                report.s3_url = pdf_url
            except Exception as e:
                logger.error(f"[PDPA] S3 upload failed for {report_id}: {e}")

        # Mark report completed
        report.status = "completed"
        report.completed_at = datetime.utcnow()
        assessment["pdf_generated"] = True
        assessment["pdf_url"] = pdf_url
        assessment["on_page_only"] = False
        report.assessment_data = assessment
        from sqlalchemy.orm.attributes import flag_modified as _flag
        _flag(report, "assessment_data")
        db.commit()

        # ── Step 4: Update vendor compliance score (+8 to +25 pts) ─────────
        vendor_id = str(report.owner_id)
        try:
            from app.services.scoring import VendorScoreEngine
            VendorScoreEngine.update_vendor_score(db, vendor_id)
            logger.info(f"[PDPA] Vendor score updated for vendor {vendor_id}")
        except Exception as e:
            logger.error(f"[PDPA] Score update failed for vendor {vendor_id}: {e}")

        # ── Step 5: Write CertificateLog ────────────────────────────────────
        try:
            from app.core.models_v10 import CertificateLog
            cert = CertificateLog(
                vendor_id=report.owner_id,
                certificate_type="PDPA",
                report_id=report.id,
                file_key=report.file_key,
                generated_at=datetime.utcnow(),
            )
            db.add(cert)
            db.commit()
        except Exception as e:
            logger.error(f"[PDPA] CertificateLog failed for {report_id}: {e}")

        # ── Step 6: Email PDF to vendor ────────────────────────────────────
        if contact_email:
            try:
                download_section = (
                    f'<p style="margin-top:24px;">'
                    f'<a href="{pdf_url}" style="background-color:#10b981;color:#fff;'
                    f'padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;'
                    f'display:inline-block;">Download PDPA Snapshot Report (PDF)</a></p>'
                    if pdf_url
                    else "<p>Your report will be available on the BOOPPA dashboard shortly.</p>"
                )
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:20px;">Your PDPA Snapshot is Ready</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>Hello <strong>{company_name}</strong>,</p>
                    <p>Your PDPA Snapshot report for <strong>{website_url}</strong> has been generated.</p>
                    <p>The report evaluates your compliance across 8 PDPA dimensions — consent, data flow,
                       DSAR procedures, breach notification, retention, third-party processors, DPO, and
                       privacy notice — and provides specific recommendations with legislative references.</p>
                    <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;
                                border-radius:4px;margin:20px 0;">
                      <strong>Compliance Score:</strong> {100 - int(risk_score or 50)}/100<br>
                      <strong>Report ID:</strong> {report_id[:8].upper()}<br>
                      <strong>Generated:</strong> {datetime.utcnow().strftime('%d %B %Y')}
                    </div>
                    <p>Your compliance score on BOOPPA has been updated to reflect this scan.
                       Procurement officers searching for verified vendors will see your improved standing.</p>
                    {download_section}
                    <p style="margin-top:24px;">
                      <a href="https://www.booppa.io/vendor/dashboard"
                         style="color:#10b981;text-decoration:underline;">View your dashboard →</a>
                    </p>
                    <p style="color:#64748b;font-size:11px;margin-top:24px;">
                      This report is for informational purposes only and does not constitute legal advice
                      or PDPC certification. BOOPPA is not a law firm.
                    </p>
                  </div>
                </body></html>
                """
                email_svc = EmailService()
                await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your PDPA Snapshot Report is Ready — {company_name}",
                    body_html=body_html,
                )
            except Exception as e:
                logger.error(f"[PDPA] Email failed for {contact_email}: {e}")

        logger.info(f"[PDPA] Fulfilled {report_id} for vendor {vendor_id} pdf={pdf_url}")
    except Exception as e:
        logger.error(f"[PDPA] Fulfillment error for {report_id}: {e}")
        db.rollback()
    finally:
        db.close()


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
        from app.core.models_v6 import VerifyRecord, LifecycleStatus, VendorSector, VendorScore
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
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:18px;">You Were Shortlisted</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>A procurement team is actively evaluating vendors in the <strong>{sector}</strong> sector for a new opportunity.</p>
                    <p>Your verified status on BOOPPA placed you in the <strong>top 5 shortlisted vendors</strong> for this opportunity.</p>
                    <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;border-radius:4px;margin:20px 0;">
                      <strong>Opportunity:</strong> {buyer_rfp_title or 'New procurement in your sector'}<br>
                      <strong>Your sector:</strong> {sector}<br>
                      <strong>Buyer:</strong> Identity confidential — standard procurement practice
                    </div>
                    <p>To improve your position in future shortlists, strengthen your evidence package:</p>
                    <p>
                      <a href="https://www.booppa.io/vendor/dashboard" style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
                        View Dashboard →
                      </a>
                    </p>
                    <p style="color:#64748b;font-size:11px;margin-top:24px;">
                      You are receiving this because your vendor profile is verified on BOOPPA.<br>
                      Buyer details are kept confidential per procurement best practice.
                    </p>
                  </div>
                </body></html>
                """
                await email_svc.send_html_email(
                    to_email=user.email,
                    subject="You Were Shortlisted — New Procurement Opportunity",
                    body_html=body_html,
                )
                logger.info(f"[Strategy6] Notified vendor {user.email} for sector {sector}")
            except Exception as e:
                logger.warning(f"[Strategy6] Email failed for {user.email}: {e}")

    except Exception as e:
        logger.error(f"[Strategy6] Failed: {e}")
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
            # Subscriptions have no report — activate directly
            if product_type in SUBSCRIPTION_PRODUCT_TYPES:
                stripe_sub_id = session.get("subscription")
                stripe_cust_id = session.get("customer")
                import asyncio as _aio
                _aio.create_task(_activate_subscription(
                    product_type=product_type,
                    customer_email=customer_email,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_customer_id=stripe_cust_id,
                ))
                logger.info(f"Queued subscription activation for {product_type} email={customer_email}")
                return {"received": True}

            # Bundles are self-contained — fan out to component fulfillment tasks
            if product_type in BUNDLE_COMPONENTS:
                import asyncio as _aio
                _aio.create_task(_fulfill_bundle(
                    product_type=product_type,
                    report_id=None,
                    customer_email=customer_email,
                    metadata=metadata,
                    session_id=session.get("id"),
                ))
                logger.info(f"Queued bundle fulfillment for {product_type} email={customer_email}")
                return {"received": True}

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

            # Vendor Proof — create VerifyRecord + VendorStatusSnapshot + send badge email
            if product_type in VENDOR_PROOF_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_vendor_proof_task
                fulfill_vendor_proof_task.delay(str(report.id), customer_email)
                logger.info(f"Queued vendor_proof fulfillment for report {report_id}")

            # PDPA Snapshot — scan, PDF, score update, CertificateLog, email
            elif product_type in PDPA_PRODUCT_TYPES:
                from app.workers.tasks import fulfill_pdpa_task
                fulfill_pdpa_task.delay(str(report.id), customer_email)
                logger.info(f"Queued pdpa fulfillment for report {report_id}")

            # Notarization — lightweight anchor + certificate (no AI, no website scan)
            elif product_type in NOTARIZATION_PRODUCT_TYPES:
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

                    # Strategy 6: notify top-5 verified sector peers (rfp_express only)
                    if product_type == "rfp_express":
                        sector = metadata.get("sector") or (intake_dict or {}).get("sector")
                        rfp_title = rfp_desc or metadata.get("rfp_description") or "New procurement opportunity"
                        import asyncio as _asyncio
                        _asyncio.create_task(_fire_strategy_6(sector, rfp_title))

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

    # ── Subscription lifecycle events ────────────────────────────────────────
    if event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        raw = json.loads(payload)
        sub = raw.get("data", {}).get("object", {})
        stripe_sub_id  = sub.get("id")
        stripe_cust_id = sub.get("customer")
        sub_status     = sub.get("status")
        cust_email = None
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            cust = stripe.Customer.retrieve(stripe_cust_id) if stripe_cust_id else None
            cust_email = (cust or {}).get("email") if isinstance(cust, dict) else getattr(cust, "email", None)
        except Exception:
            pass

        if stripe_sub_id and sub_status in ("active", "trialing"):
            items = sub.get("items", {}).get("data", [])
            product_type_sub = None
            import os as _os
            for item in items:
                price_id = (item.get("price") or {}).get("id") or (item.get("plan") or {}).get("id")
                if price_id:
                    for key in ("vendor_active_monthly", "vendor_active_annual",
                                "pdpa_monitor_monthly", "pdpa_monitor_annual",
                                "enterprise_monthly", "enterprise_pro_monthly"):
                        env_price = (
                            _os.environ.get(f"STRIPE_{key.upper()}")
                            or _os.environ.get(f"NEXT_PUBLIC_STRIPE_{key.upper()}")
                        )
                        if env_price and env_price == price_id:
                            product_type_sub = key
                            break
            if product_type_sub and cust_email:
                import asyncio as _aio2
                _aio2.create_task(_activate_subscription(
                    product_type=product_type_sub,
                    customer_email=cust_email,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_customer_id=stripe_cust_id,
                ))
                logger.info(f"[Webhook] Subscription {event['type']} → plan={product_type_sub} email={cust_email}")

        elif sub_status == "canceled":
            _db3 = SessionLocal()
            try:
                if cust_email:
                    user = _db3.query(User).filter(User.email == cust_email).first()
                    if user:
                        user.plan = "free"
                        if hasattr(user, "stripe_subscription_id"):
                            user.stripe_subscription_id = None
                        _db3.commit()
                        logger.info(f"[Webhook] Subscription canceled — downgraded {cust_email} to free")
            except Exception as exc:
                logger.error(f"[Webhook] Cancellation handling failed: {exc}")
            finally:
                _db3.close()

    # ── Invoice renewal — trigger monthly health checks ───────────────────────
    if event["type"] == "invoice.payment_succeeded":
        raw = json.loads(payload)
        inv = raw.get("data", {}).get("object", {})
        if inv.get("billing_reason") in ("subscription_cycle", "subscription_update"):
            cust_email_inv = inv.get("customer_email")
            if cust_email_inv:
                _db4 = SessionLocal()
                try:
                    user = _db4.query(User).filter(User.email == cust_email_inv).first()
                    if user and getattr(user, "plan", "") == "vendor_active":
                        from app.workers.tasks import vendor_active_health_check_task
                        vendor_active_health_check_task.delay(str(user.id), cust_email_inv)
                        logger.info(f"[Webhook] Queued monthly health check for {cust_email_inv}")
                except Exception as exc:
                    logger.error(f"[Webhook] Invoice renewal hook failed: {exc}")
                finally:
                    _db4.close()

    return {"received": True}
