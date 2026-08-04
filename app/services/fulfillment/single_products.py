from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.demo_flags import is_demo_anchor
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service
from app.services.tx_utils import is_real_onchain_tx

from app.services.fulfillment.helpers import (
    _create_stub_report,
    _alert_payment_fulfillment_issue,
    _maybe_fire_cover_sheet,
    _fire_strategy_6,
    _log_purchase_activity,
)

from app.services.email_service import EmailService
from app.services.email_layout import (
    branded_email_html,
    email_button,
    email_info_box,
    email_kv,
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


async def _defer_rfp_to_intake(
    *,
    rfp_product_type: str,
    bundle_source: str,
    customer_email: str | None,
    vendor_url: str | None,
    company_name: str | None,
    session_id: str | None,
) -> str | None:
    """Create a PendingRfpIntake row and email the buyer a link to /rfp-intake/{id}.

    Used when an RFP-bearing purchase arrives without a `rfp_description` — bundles
    bought from /pricing, plus standalone rfp_express/rfp_complete one-click buys.
    Caller need not commit; this helper owns its own session. Returns intake_id
    or None if no user could be resolved.
    """
    if not customer_email:
        await _alert_payment_fulfillment_issue(
            reason="RFP purchase paid but webhook had no customer_email",
            product_type=rfp_product_type,
            customer_email=None,
            session_id=session_id,
            extra={"bundle_source": bundle_source},
            notify_customer=False,
        )
        return None
    from app.core.models import PendingRfpIntake

    db = SessionLocal()
    intake_id: str | None = None
    try:
        user = db.query(User).filter(User.email == customer_email).first()
        if not user:
            await _alert_payment_fulfillment_issue(
                reason="RFP purchase paid but no user row matched customer_email",
                product_type=rfp_product_type,
                customer_email=customer_email,
                session_id=session_id,
                extra={"bundle_source": bundle_source},
            )
            return None
        resolved_url = vendor_url or (getattr(user, "website", "") or "") or None
        # Prefer the company entered for THIS purchase. The account fallback is only
        # legitimate for an account that can be its own subject (a vendor
        # self-assessing) — a buyer or CSP manages many subjects, so their own
        # company is not a stand-in for an unnamed one. `None` leaves the intake
        # form blank for the buyer to fill, which is the honest state; it never
        # falls back to the cached legal_name either.
        from app.services.evidence_enricher import account_may_self_subject

        resolved_company = company_name or (
            ((getattr(user, "company", "") or "").strip() or None)
            if account_may_self_subject(user)
            else None
        )
        pending = PendingRfpIntake(
            user_id=user.id,
            session_id=session_id,
            rfp_product_type=rfp_product_type,
            bundle_source=bundle_source,
            vendor_url=resolved_url,
            company_name=resolved_company,
            status="pending",
        )
        db.add(pending)
        db.flush()
        intake_id = str(pending.id)
        db.commit()
        logger.info(
            f"[RFP-defer:{rfp_product_type}] Created PendingRfpIntake {intake_id} for {customer_email}"
        )
    except Exception as e:
        logger.error(f"[RFP-defer:{rfp_product_type}] DB error: {e}")
        db.rollback()
        await _alert_payment_fulfillment_issue(
            reason=f"RFP defer-to-intake DB error: {type(e).__name__}: {e}",
            product_type=rfp_product_type,
            customer_email=customer_email,
            session_id=session_id,
            extra={"bundle_source": bundle_source}
        )
        raise
    finally:
        db.close()

    try:
        kit_label = (
            "RFP Complete Kit"
            if rfp_product_type == "rfp_complete"
            else "RFP Express Kit"
        )
        intake_url = f"https://www.booppa.io/rfp-intake/{intake_id}"
        sent = await EmailService().send_html_email(
            to_email=customer_email,
            subject=f"One more step: complete your {kit_label} brief",
            body_html=branded_email_html(
                f"""
              <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Tell us about your RFP</h2>
              <p style="margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;">
                Thanks for your purchase. Share a few details about the procurement and
                we'll generate your <strong>{kit_label}</strong>.
              </p>
              {email_button(intake_url, "Complete your RFP brief")}
              <p style="margin:0;color:#64748b;font-size:13px;">
                Takes about 2 minutes. Your kit is generated as soon as you submit.
              </p>
                """,
                title="Complete your RFP brief",
                preheader=f"Share a few details and we'll generate your {kit_label}.",
            ),
        )
        # send_html_email returns False on Resend/SES rejection without
        # raising — the buyer would never see the brief link and the page
        # would sit on "Confirming purchase…". Surface it via the standard
        # fulfillment alert so it isn't silently lost.
        if not sent:
            logger.error(
                f"[RFP-defer:{rfp_product_type}] Intake email rejected by provider "
                f"for {customer_email} (intake_id={intake_id})"
            )
            await _alert_payment_fulfillment_issue(
                reason="Intake email rejected by email provider",
                product_type=rfp_product_type,
                customer_email=customer_email,
                session_id=session_id,
                extra={"intake_id": intake_id, "bundle_source": bundle_source},
                notify_customer=False,
            )
        else:
            logger.info(
                f"[RFP-defer:{rfp_product_type}] Sent intake email to {customer_email} "
                f"(intake_id={intake_id})"
            )
    except Exception as email_err:
        logger.warning(
            f"[RFP-defer:{rfp_product_type}] Intake email failed: {email_err}"
        )

    return intake_id


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

        assessment = (
            report.assessment_data if isinstance(report.assessment_data, dict) else {}
        )
        file_hash = assessment.get("file_hash") or report.audit_hash
        original_filename = assessment.get("original_filename", "document")
        file_size = assessment.get("file_size_bytes")
        hash_algorithm = assessment.get("hash_algorithm", "SHA-256")
        mime_type = assessment.get("mime_type")
        document_descriptor = assessment.get("document_descriptor")
        contact_email = (
            customer_email
            or assessment.get("contact_email")
            or assessment.get("customer_email")
        )

        # Step 1: Anchor file hash on blockchain
        # Admin test-checkout reports use a mock tx hash (no gas) — never spend
        # real gas from the shared wallet on QA runs.
        demo_anchor = is_demo_anchor(assessment=assessment)
        tx_hash = None
        try:
            blockchain = BlockchainService()
            tx_hash = await blockchain.anchor_evidence(
                file_hash, metadata=f"notarization:{report_id}", demo=demo_anchor
            )
            # Only store a real hex tx_hash; None means already anchored (no new tx)
            if tx_hash:
                report.tx_hash = tx_hash
            else:
                # Hash already on-chain (e.g. duplicate file upload). Inherit the
                # tx_hash from the prior report that anchored this same hash so
                # this report is correctly counted as anchored and the receipt
                # email/PDF can include a Polygonscan link.
                prior = (
                    db.query(Report)
                    .filter(
                        Report.audit_hash == file_hash,
                        Report.id != report_id,
                        Report.tx_hash.isnot(None),
                        Report.tx_hash != "already_anchored",
                    )
                    .order_by(Report.created_at.asc())
                    .first()
                )
                if prior and prior.tx_hash:
                    tx_hash = prior.tx_hash
                    report.tx_hash = tx_hash
                    logger.info(
                        f"[Notarize] Hash already anchored — inherited tx={tx_hash} "
                        f"from prior report {prior.id}"
                    )
            report.audit_hash = file_hash  # keep as original file hash for verification
            # Only claim anchored when we actually hold a tx — either freshly
            # mined or inherited from the prior report above. Stamping this
            # unconditionally marked reports as anchored-at-<now> with a NULL
            # tx_hash when the anchor returned nothing.
            if tx_hash:
                assessment["blockchain_anchored"] = True
                assessment["blockchain_anchored_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
            report.assessment_data = assessment
            flag_modified(report, "assessment_data")
            db.commit()
            logger.info(f"[Notarize] Anchored {file_hash[:16]}… tx={tx_hash}")
        except Exception as e:
            logger.error(f"[Notarize] Blockchain anchor failed for {report_id}: {e}")

        # Step 2: Build verify URL
        verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{file_hash}"
        polygonscan_url = (
            f"{settings.active_polygon_explorer_url.rstrip('/')}/tx/{tx_hash}"
            if is_real_onchain_tx(tx_hash)
            else None
        )

        # Step 3: Generate notarization certificate PDF
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            from app.services.evidence_enricher import resolve_report_legal_name
            _company_name = await resolve_report_legal_name(report, db) or (
                report.company_name or "Your Organisation"
            )
            pdf_data = {
                "report_id": report_id,
                "framework": "compliance_notarization",
                "company_name": _company_name,
                "created_at": (
                    report.created_at.isoformat()
                    if report.created_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                "status": "completed",
                "tx_hash": tx_hash,
                "audit_hash": file_hash,
                "original_filename": original_filename,
                "file_size": file_size,
                "hash_algorithm": hash_algorithm,
                "mime_type": mime_type,
                "document_descriptor": document_descriptor,
                "verify_url": verify_url,
                "polygonscan_url": polygonscan_url,
                "proof_header": "BOOPPA-PROOF-SG",
                "schema_version": "1.0",
                "network": settings.active_polygon_network_name,
                "testnet_notice": settings.blockchain_notice,
                "payment_confirmed": True,
                "tier": "pro",
                "contact_email": contact_email,
                "base_url": "https://www.booppa.io",
            }
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
            assessment["pdf_generated"] = True
            assessment["pdf_generated_at"] = datetime.now(timezone.utc).isoformat()
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
                assessment["s3_uploaded_at"] = datetime.now(timezone.utc).isoformat()
                assessment["verify_url"] = verify_url
                assessment["polygonscan_url"] = polygonscan_url
                report.assessment_data = assessment
                flag_modified(report, "assessment_data")
                db.commit()
            except Exception as e:
                logger.error(f"[Notarize] S3 upload failed for {report_id}: {e}")

        # Step 5: Mark completed
        report.status = "completed"
        report.completed_at = datetime.now(timezone.utc)
        db.commit()

        # Step 6: Send email (guard against duplicate sends on retry)
        already_emailed = assessment.get("notarization_email_sent")
        if contact_email and not already_emailed:
            try:
                email_svc = EmailService()
                download_section = (
                    email_button(pdf_url, "Download Notarization Certificate (PDF)")
                    if pdf_url
                    else '<p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your certificate will be available on the BOOPPA website once processing is complete.</p>'
                )
                tx_line = (
                    email_info_box(
                        f'<strong>Blockchain TX:</strong> <a href="{polygonscan_url}" '
                        f'style="color:#10b981;word-break:break-all;">{tx_hash or ""}</a>',
                        tone="success",
                    )
                    if polygonscan_url else ""
                )
                body_html = branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Notarization Certificate is Ready</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hello {report.company_name or "Customer"},</p>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your blockchain notarization certificate for
                     <strong>{original_filename}</strong> has been generated.</p>
                  {email_kv([("SHA-256 Hash", f"<code>{file_hash}</code>")])}
                  {tx_line}
                  {download_section}
                  <p style="margin:20px 0 0;color:#64748b;font-size:12px;">
                    Certificate ID: {report_id}<br>
                    Network: {settings.active_polygon_network_name}
                  </p>
                  <p style="margin:16px 0 0;color:#334155;font-size:15px;">Thank you for using BOOPPA.</p>
                    """,
                    title="Notarization certificate ready",
                    preheader=f"Your notarization certificate for {original_filename} is ready.",
                )
                sent = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Notarization Certificate is Ready — {original_filename}",
                    body_html=body_html,
                    attachments=[(f"Notarization_{report_id[:8]}.pdf", pdf_bytes)] if pdf_bytes else None,
                )
                if sent:
                    # Mark email as sent to prevent duplicates on retry. Only on a
                    # confirmed send — a False return must leave the flag unset so
                    # the retry path can re-deliver the certificate.
                    assessment["notarization_email_sent"] = True
                    assessment["notarization_email_sent_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    report.assessment_data = assessment
                    flag_modified(report, "assessment_data")
                    db.commit()
                else:
                    await _alert_payment_fulfillment_issue(
                        reason="notarization certificate email rejected by provider",
                        product_type="notarization",
                        customer_email=contact_email,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[Notarize] Email failed for {report_id}: {e}")

        # Step 7: Update elevation metadata so CAL advances to NOTARIZED
        try:
            from app.services.notarization_elevation import create_or_update_elevation
            from app.core.models import User
            from app.core.models import VerifyRecord, Proof

            # Resolve real user from contact_email (report.owner_id may be a random UUID
            # if the report was created via the public unauthenticated endpoint)
            real_vendor_id = None
            if contact_email:
                real_user = db.query(User).filter(User.email == contact_email).first()
                if real_user:
                    real_vendor_id = str(real_user.id)
                    # Also remap the report so future lookups are consistent
                    if str(report.owner_id) != real_vendor_id:
                        report.owner_id = real_user.id
                        db.flush()

            vendor_id = real_vendor_id or str(report.owner_id)

            # create_or_update_elevation counts Proof records linked to the vendor's
            # VerifyRecord. The notarization flow doesn't create Proof rows, so we
            # create one now to represent this completed notarization.
            verify = (
                db.query(VerifyRecord)
                .filter(VerifyRecord.vendor_id == vendor_id)
                .first()
            )
            if verify and file_hash:
                existing_proof = (
                    db.query(Proof)
                    .filter(
                        Proof.verify_id == verify.id,
                        Proof.hash_value == file_hash,
                    )
                    .first()
                )
                if not existing_proof:
                    proof = Proof(
                        verify_id=verify.id,
                        hash_value=file_hash,
                        title=original_filename or "Notarized Document",
                        compliance_score=5,
                        metadata_json={
                            "report_id": report_id,
                            "tx_hash": tx_hash,
                            "notarized_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    db.add(proof)
                    db.commit()
                    logger.info(
                        f"[Notarize] Created Proof record for vendor {vendor_id}"
                    )

            create_or_update_elevation(db, vendor_id)
            logger.info(f"[Notarize] Elevation metadata updated for vendor {vendor_id}")

            # Sync VerifyRecord.verification_level to match the new depth so the
            # compliance score weight (1.0×BASIC → 1.1×STANDARD) updates correctly.
            try:
                from app.core.models import (
                    VerifyRecord as _VR,
                    VerificationLevel as _VL,
                )
                from app.services.vendor_status import compute_verification_depth

                _new_depth = compute_verification_depth(db, vendor_id)
                _depth_to_level = {
                    "STANDARD": _VL.STANDARD,
                    "DEEP": _VL.PREMIUM,
                    "CERTIFIED": _VL.GOVERNMENT,
                    "ENTERPRISE": _VL.GOVERNMENT,
                }
                _new_level = _depth_to_level.get(_new_depth)
                if _new_level:
                    _vr = db.query(_VR).filter(_VR.vendor_id == vendor_id).first()
                    if _vr and _vr.verification_level != _new_level:
                        _vr.verification_level = _new_level
                        db.commit()
                        logger.info(
                            f"[Notarize] VerifyRecord.verification_level → {_new_level.value} for {vendor_id}"
                        )
            except Exception as lvl_err:
                logger.warning(
                    f"[Notarize] verification_level sync failed for {vendor_id}: {lvl_err}"
                )

            # Record fulfillment so Engagement + Recency move on each notarization.
            _log_purchase_activity(
                db,
                vendor_id,
                activity_type="NOTARIZATION_FULFILLED",
                description=f"Notarization fulfilled: report={report_id}",
                extra={"report_id": str(report_id), "tx_hash": tx_hash},
            )

            # Recalculate compliance score so the dashboard reflects the new document.
            try:
                from app.services.scoring import VendorScoreEngine

                VendorScoreEngine.update_vendor_score(db, vendor_id)
                logger.info(f"[Notarize] Vendor score recalculated for {vendor_id}")
            except Exception as score_err:
                logger.warning(
                    f"[Notarize] Score update failed for {vendor_id}: {score_err}"
                )

            # Refresh the procurement snapshot so tender win probability
            # reflects the newly created Proof and elevation data.
            try:
                from app.services.vendor_status import upsert_status_snapshot

                upsert_status_snapshot(db, vendor_id)
                logger.info(
                    f"[Notarize] Status snapshot refreshed for vendor {vendor_id}"
                )
            except Exception as snap_err:
                logger.warning(
                    f"[Notarize] Snapshot refresh failed for {vendor_id}: {snap_err}"
                )
        except Exception as e:
            logger.warning(f"[Notarize] Elevation update failed for {report_id}: {e}")

        logger.info(f"[Notarize] Fulfilled {report_id}: tx={tx_hash} pdf={pdf_url}")
        _maybe_fire_cover_sheet(contact_email, user_id=vendor_id, test_simulation=is_demo_anchor(assessment=assessment))
    except Exception as e:
        # A failure in the core steps (commit/anchor/upload) previously only logged
        # and returned, so the buyer paid, no certificate email went out, the Celery
        # task saw "success" and never retried, and nobody was alerted — a silent
        # paid-but-unfulfilled notarization. Mirror the Vendor Proof handler: roll
        # back, page support, and re-raise so `fulfill_notarization_task` retries.
        logger.error(f"[Notarize] Fulfillment error for {report_id}: {e}")
        db.rollback()
        try:
            await _alert_payment_fulfillment_issue(
                reason=f"Notarization fulfillment raised before delivery: {e}",
                product_type="compliance_notarization_1",
                customer_email=customer_email,
                extra={"report_id": report_id},
            )
        except Exception as _alert_err:
            logger.error(f"[Notarize] Alert dispatch failed for {report_id}: {_alert_err}")
        raise
    finally:
        db.close()


class _RfpDeliverableIncomplete(Exception):
    """Raised when an RFP kit built successfully but a tier-defining deliverable
    (the editable DOCX for rfp_complete) is missing. Propagated past the
    swallow-all handler so ``fulfill_rfp_task`` retries instead of shipping a
    PDF-only kit for the SGD 599 Complete tier."""


async def _fulfill_rfp_package(
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
    session_id: str | None = None,
    intake_data: dict | None = None,
    allow_incomplete: bool = False,
) -> None:
    """Background task: generate and deliver the RFP Kit package after payment."""
    db = SessionLocal()
    try:
        from app.services.rfp_express_builder import RFPExpressBuilder

        rfp_details = {"description": rfp_description} if rfp_description else None
        if intake_data:
            rfp_details = {**(rfp_details or {}), "intake": intake_data}
        builder = RFPExpressBuilder(
            vendor_id=vendor_id, vendor_email=vendor_email, session_id=session_id
        )
        result = await builder.generate_express_package(
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_details=rfp_details,
            db=db,
            product_type=product_type,
            allow_incomplete=allow_incomplete,
        )
        # HARD GATE (audit fix): the builder blocks delivery when any
        # [Verify:]/[FILL IN] placeholder survives intake substitution. Do NOT
        # persist a completed Report, do NOT cache an rfp_result — instead route
        # the buyer back to the intake form to supply the missing facts, then
        # they resubmit and we regenerate.
        if result.get("blocked"):
            await _handle_blocked_rfp(
                db=db,
                product_type=product_type,
                vendor_email=vendor_email,
                session_id=session_id,
                missing_fields=result.get("missing_fields") or [],
                residual_placeholders=result.get("residual_placeholders") or 0,
                company_name=company_name,
            )
            return

        download_url = result.get("download_url")
        logger.info(
            f"RFP package fulfilled: product={product_type} vendor={vendor_id} "
            f"url={download_url} errors={result.get('errors')}"
        )

        # Complete-tier hard deliverable gate (audit fix). The editable DOCX is
        # the defining extra of rfp_complete (SGD 599 vs SGD 249 Express). If it
        # failed to build or upload, `docx_url` comes back None and the kit would
        # previously ship PDF-only, silently — the buyer pays for the Complete
        # tier and never gets its distinguishing deliverable. Refuse to persist /
        # cache / email a PDF-only kit: alert support and raise so the Celery
        # task retries. The test/admin-sim path (allow_incomplete) is exempt so
        # the e2e harness still yields a kit without a live S3 bucket.
        if (
            product_type == "rfp_complete"
            and not allow_incomplete
            and not result.get("docx_url")
        ):
            logger.error(
                "[RFP] rfp_complete for %s produced no docx_url — refusing PDF-only "
                "delivery; will retry. errors=%s warnings=%s",
                vendor_email, result.get("errors"), result.get("warnings"),
            )
            try:
                await _alert_payment_fulfillment_issue(
                    reason="RFP Complete kit generated without the editable DOCX deliverable",
                    product_type=product_type,
                    customer_email=vendor_email,
                    session_id=session_id,
                    notify_customer=False,
                )
            except Exception as _ae:
                logger.warning("[RFP] docx-missing alert failed: %s", _ae)
            raise _RfpDeliverableIncomplete(
                f"rfp_complete missing docx_url (session={session_id})"
            )

        # Persist a Report row for every completed RFP so the bundle progress
        # page (and any future audit query) can find it. The cover-sheet
        # auto-fire is still gated on pending_cover_sheet below.
        if vendor_email:
            try:
                ce_user = db.query(User).filter(User.email == vendor_email).first()
                if ce_user:
                    from datetime import datetime as _dt, timezone as _tz

                    rfp_report = Report(
                        owner_id=ce_user.id,
                        framework="rfp_complete",
                        company_name=company_name or "Your Organisation",
                        company_website=vendor_url,
                        assessment_data={
                            "product_type": product_type,
                            "download_url": download_url,
                            "s3_key": result.get("pdf_s3_key"),
                            "docx_url": result.get("docx_url"),
                            "declaration_url": result.get("declaration_url"),
                            "appendix_d_url": result.get("appendix_d_url"),
                            # Persist the full Q&A list (not just the count) so
                            # the Compliance Cover Sheet can embed it later —
                            # the result cache expires, the Report row doesn't.
                            "qa_answers": result.get("qa_answers", []) or [],
                            "qa_count": len(result.get("qa_answers", []) or []),
                            "answer_source": result.get("answer_source"),
                            "discrepancies": result.get("discrepancies") or [],
                            "file_hash": result.get("file_hash"),
                            "data_sources": result.get("data_sources") or {},
                            "generated_at": result.get("generated_at"),
                            "polygonscan_url": result.get("polygonscan_url"),
                            # Persist the buyer's confirmed inputs so monthly
                            # refresh emails (compliance_evidence_monthly) can
                            # pre-fill /rfp-intake/{new_id} with last month's
                            # answers — 30-second re-confirm instead of full re-entry.
                            "intake_rfp_description": rfp_description or "",
                            "intake_data": intake_data or {},
                        },
                        status="completed",
                        tx_hash=result.get("tx_hash"),
                        completed_at=_dt.now(_tz.utc),
                    )
                    db.add(rfp_report)
                    if getattr(ce_user, "pending_cover_sheet", False):
                        ce_user.compliance_evidence_rfp_ready = True
                    db.commit()
                    if getattr(ce_user, "pending_cover_sheet", False):
                        _maybe_fire_cover_sheet(vendor_email, user_id=vendor_id, test_simulation=is_demo_anchor(session_id=session_id))
            except Exception as flag_err:
                logger.warning(
                    f"[RFP→CoverSheet] Could not record RFP completion for {vendor_email}: {flag_err}"
                )
        # Store result keyed by session_id so the result page can retrieve it
        if session_id and download_url:
            from app.core.cache import cache as cache_mod

            cache_mod.set(
                cache_mod.cache_key(f"rfp_result:{session_id}"),
                {
                    "download_url": download_url,
                    "docx_url": result.get("docx_url"),
                    "declaration_url": result.get("declaration_url"),
                    "appendix_d_url": result.get("appendix_d_url"),
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
    except _RfpDeliverableIncomplete:
        # Re-raise so fulfill_rfp_task's retry/backoff kicks in — do NOT swallow
        # into a success like the generic handler below.
        raise
    except Exception as e:
        # Any other failure (scrape/AI/S3/DB) previously logged and returned, so
        # fulfill_rfp_task saw "success" and never retried. If the failure preceded
        # the rfp_result cache write, the result page polls "Generating…" forever
        # and — for Evidence Pack bundles — the cover sheet never fires. Page
        # support and re-raise so the task retries instead of silently dropping the kit.
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {e}")
        try:
            await _alert_payment_fulfillment_issue(
                reason=f"RFP kit fulfillment raised before delivery: {e}",
                product_type="rfp_complete",
                customer_email=vendor_email,
                extra={"vendor_id": str(vendor_id)},
            )
        except Exception as _alert_err:
            logger.error(f"[RFP] Alert dispatch failed for vendor {vendor_id}: {_alert_err}")
        raise
    finally:
        db.close()


async def _handle_blocked_rfp(
    db,
    product_type: str,
    vendor_email: str,
    session_id: str | None,
    missing_fields: list[str],
    residual_placeholders: int,
    company_name: str,
) -> None:
    """An RFP kit was blocked at the hard placeholder gate. Route the buyer back
    to intake to supply the missing facts (status -> needs_more_info), email them
    the exact fields to complete, and alert ops. No kit is delivered or cached.
    """
    from app.core.models import PendingRfpIntake

    intake_id = None
    try:
        row = None
        if session_id:
            row = (
                db.query(PendingRfpIntake)
                .filter(PendingRfpIntake.session_id == session_id)
                .order_by(PendingRfpIntake.created_at.desc())
                .first()
            )
        if row is None and vendor_email:
            # Fall back to this buyer's most recent submitted/pending intake.
            buyer = db.query(User).filter(User.email == vendor_email).first()
            if buyer:
                row = (
                    db.query(PendingRfpIntake)
                    .filter(PendingRfpIntake.user_id == buyer.id)
                    .order_by(PendingRfpIntake.created_at.desc())
                    .first()
                )
        if row is not None:
            row.status = "needs_more_info"
            db.commit()
            intake_id = str(row.id)
    except Exception as flip_err:
        logger.warning(f"[RFP] Could not flip intake to needs_more_info: {flip_err}")

    # Email the buyer the precise fields to complete, with a link back to intake.
    try:
        import html as _html
        from app.services.email_service import EmailService

        intake_link = (
            f"https://www.booppa.io/rfp-intake/{intake_id}" if intake_id else None
        )
        fields_html = "".join(
            f"<li style=\"margin:4px 0;\">{_html.escape(f)}</li>" for f in missing_fields
        ) or "<li>Some verification details were missing.</li>"
        cta_html = (
            email_button(intake_link, "Complete your RFP brief")
            if intake_link else
            '<p style="margin:0 0 16px;color:#334155;font-size:15px;">Please return to your RFP brief on booppa.io to complete the missing details.</p>'
        )
        from app.services.email_templates import get_rfp_kit_needs_info_html
        body_html = get_rfp_kit_needs_info_html(company_name, fields_html, cta_html)
        sent = await EmailService().send_html_email(
            to_email=vendor_email,
            subject="Action needed: complete your RFP Kit details",
            body_html=body_html,
        )
        if not sent:
            logger.error(f"[RFP] needs-more-info email rejected for {vendor_email}")
    except Exception as mail_err:
        logger.warning(f"[RFP] Could not send needs-more-info email: {mail_err}")

    # Ops visibility.
    try:
        await _alert_payment_fulfillment_issue(
            reason=(
                f"RFP kit BLOCKED at hard placeholder gate — {residual_placeholders} "
                "unfilled [Verify:]/[FILL IN] field(s); buyer routed back to intake"
            ),
            product_type=product_type,
            customer_email=vendor_email,
            session_id=session_id,
            extra={
                "residual_placeholders": residual_placeholders,
                "missing_fields": missing_fields,
                "company_name": company_name,
                "intake_id": intake_id,
            },
            notify_customer=False,
        )
    except Exception as alert_err:
        logger.warning(f"[RFP] Block alert failed (non-blocking): {alert_err}")


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
        from app.core.models import (
            VerifyRecord,
            LifecycleStatus,
            VerificationLevel,
            VendorScore,
            VendorSector,
        )
        from app.core.models import VendorStatusSnapshot

        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[VendorProof] Report {report_id} not found")
            return

        vendor_id = report.owner_id
        contact_email = customer_email or (report.assessment_data or {}).get(
            "contact_email"
        )

        # If the report was created via the public endpoint, owner_id is a random UUID.
        # Resolve the real user by email so VerifyRecord is linked to an actual account.
        real_user = None
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
            logger.error(
                f"[VendorProof] No owner_id on report {report_id} and no user resolved from email"
            )
            return
        # Certify the entity THIS report verifies, not the buyer account's cached
        # identity — a reused account's stale legal_name must never be stamped onto
        # a different vendor's proof.
        from app.services.evidence_enricher import resolve_report_legal_name
        company_name = await resolve_report_legal_name(report, db) or (report.company_name or "Vendor")
        verify_url = f"https://www.booppa.io/verify/{report_id}"

        # ── Honest score, computed BEFORE we persist it ─────────────────────
        # Derive the standing from the vendor's ACTUAL latest PDPA scan when one
        # exists; otherwise it stays the identity-verified-only floor (30). This
        # value (vp_confidence) is the single source for VerifyRecord, the
        # snapshot, and VendorScore below — no more hardcoded 30s.
        from app.services.pdpa_findings import latest_pdpa_score

        # Report-scoped: this certificate names one subject, so only a scan of
        # that subject's website may set its standing. No allow_unattributed —
        # a certified figure must be attributable. A report with no recorded
        # website leaves the lookup account-wide, i.e. unchanged from before.
        _pdpa_compliance = latest_pdpa_score(
            db, vendor_id, domain=getattr(report, "company_website", None)
        )

        if _pdpa_compliance is None:
            vp_readiness = "CONDITIONAL"
            vp_confidence = 30.0
            vp_readiness_label = "Identity verified — compliance not yet assessed"
            vp_score_display = "Not yet assessed — run a PDPA scan to establish it"
        elif _pdpa_compliance >= 70:
            vp_readiness = "READY"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Ready"
            vp_score_display = f"{_pdpa_compliance}/100"
        elif _pdpa_compliance >= 40:
            vp_readiness = "CONDITIONAL"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Conditional"
            vp_score_display = f"{_pdpa_compliance}/100"
        else:
            vp_readiness = "NOT_READY"
            vp_confidence = float(_pdpa_compliance)
            vp_readiness_label = "Action required — critical compliance gaps"
            vp_score_display = f"{_pdpa_compliance}/100"

        _vp_score_int = int(round(vp_confidence))

        # ── ACRA registry lookup (identity attestation) ─────────────────────
        # Match the buyer's UEN against the imported ACRA registry so the
        # certificate can state real registration details instead of nothing.
        acra_info: dict = {"matched": False}
        # The subject of THIS certificate — used both to gate the account's cached
        # UEN below and as the provenance recorded on any write-back.
        _subject_company = report.company_name or company_name
        _subject_website = getattr(report, "company_website", None) or (
            (report.assessment_data or {}).get("url")
        )

        _uen = (report.assessment_data or {}).get("uen")
        if not _uen:
            from app.core.models import User
            from app.services.evidence_enricher import trusted_cached_uen

            u = db.query(User).filter(User.id == str(vendor_id)).first()
            # The account's cached UEN is only usable when its provenance matches
            # this report's subject. Reading `u.uen` raw is what certified SPQR
            # Communications' registration (21374700J) as "netpoleons" — the stale
            # UEN is a *real* UEN, so the registry lookup below succeeds and returns
            # genuine ACRA data for the wrong company. When untrusted we leave `_uen`
            # empty and fall through to the name-only lookup instead.
            _uen = (
                trusted_cached_uen(
                    u,
                    company_hint=_subject_company,
                    website_hint=_subject_website,
                )
                if u
                else None
            )

        # Whether the match started from a caller-supplied UEN (exact) vs. a
        # company-name-only lookup (fuzzy). Drives the "Matched" vs. "Matched by
        # name" label on the certificate — a name search can hit a similarly
        # named entity, so it must not claim the certainty of an exact UEN match.
        _orig_uen_present = bool(_uen)

        # Fire the registry lookup when we have a UEN (exact) OR a company name
        # (fuzzy-verified against the live dataset) — buyers without a UEN still
        # get a real ACRA match instead of "No registry match on file".
        if _uen or company_name:
            if _uen:
                try:
                    from app.core.models import DiscoveredVendor

                    _dv = (
                        db.query(DiscoveredVendor)
                        .filter(DiscoveredVendor.uen == _uen)
                        .first()
                    )
                    if _dv:
                        acra_info = {
                            "matched": True,
                            "match_type": "uen",
                            "entity_type": _dv.entity_type,
                            "registration_date": _dv.registration_date,
                            "industry": _dv.industry,
                            "source": _dv.source,
                            "registry_company_name": _dv.company_name,
                        }
                except Exception as _acra_err:
                    logger.warning("[VendorProof] ACRA lookup failed for UEN %s: %s", _uen, _acra_err)

            # Live data.gov.sg fallback — gives us the entity's current status
            # (LIVE / struck-off / ceased) and fills registration details when the
            # imported registry has no row. Without a UEN it fuzzy-matches on the
            # company name. A non-live status is surfaced on the certificate +
            # verify page rather than blocking (the sale is already paid by the
            # time this webhook runs).
            try:
                from app.services.evidence_enricher import fetch_acra_status

                _live = await fetch_acra_status(_uen, company_name=company_name)
                if _live.get("found"):
                    acra_info.setdefault("entity_type", _live.get("entity_type"))
                    acra_info.setdefault("registration_date", _live.get("registration_date"))
                    acra_info["entity_status"] = _live.get("entity_status")
                    acra_info["entity_live"] = _live.get("live")
                    # Name-only purchases arrive without a UEN. When the live
                    # fuzzy match resolves the entity, recover its official UEN
                    # so the certificate / verify record can display it.
                    if not _uen and _live.get("uen"):
                        _uen = _live.get("uen")
                        acra_info["uen"] = _uen
                    if not acra_info.get("matched"):
                        acra_info["matched"] = True
                        acra_info["match_type"] = "uen" if _orig_uen_present else "name"
                        acra_info["source"] = "data.gov.sg (live)"
                        if _live.get("registered_name"):
                            acra_info["registry_company_name"] = _live.get("registered_name")
            except Exception as _live_err:
                logger.warning("[VendorProof] live ACRA lookup failed for UEN %s: %s", _uen, _live_err)

        if acra_info.get("matched"):
            u = db.query(User).filter(User.id == str(vendor_id)).first()
            if u:
                # Cache the registry-verified identity on the account — but ONLY via
                # the sanctioned write path, which refuses when this certificate's
                # subject is not the account's own entity. The direct write that used
                # to live here is what made the SPQR contamination self-perpetuating:
                # a wrong match was written back with no provenance, so the next run
                # re-read it, re-confirmed it, and re-wrote it.
                #
                # `legal_name` is the shared canonical field every generator reads —
                # not `company`, which stays whatever the buyer typed at signup.
                from app.services.evidence_enricher import record_verified_identity

                record_verified_identity(
                    u,
                    db,
                    uen=_uen or acra_info.get("uen"),
                    legal_name=acra_info.get("registry_company_name"),
                    company_hint=_subject_company,
                    website_hint=_subject_website,
                )

        # Step 1: Create or upsert VerifyRecord
        # Vendor Proof certificates are valid for 12 months; expiry drives the
        # renewal reminder (check_vendor_proof_expiry) and the active/expired
        # badge on the public verify page.
        _vp_expires_at = datetime.now(timezone.utc) + timedelta(days=365)
        verify = (
            db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
        )
        if verify:
            verify.lifecycle_status = LifecycleStatus.ACTIVE
            verify.compliance_score = _vp_score_int
            verify.verification_level = VerificationLevel.BASIC
            verify.last_refreshed_at = datetime.now(timezone.utc)
            verify.expires_at = _vp_expires_at
            verify.company_name = company_name
        else:
            verify = VerifyRecord(
                vendor_id=vendor_id,
                company_name=company_name,
                compliance_score=_vp_score_int,
                verification_level=VerificationLevel.BASIC,
                lifecycle_status=LifecycleStatus.ACTIVE,
                expires_at=_vp_expires_at,
                correlation_id=str(report_id),
            )
            db.add(verify)
        db.flush()

        # Step 1b: Seed VendorSector from report metadata or assessment data.
        #
        # `business_sector` used to sit here as a trailing fallback and was dead:
        # nothing in the codebase ever wrote that key. It was assumed to be an
        # ACRA-derived value, but the open ACRA dataset carries no SSIC/activity
        # field, so it never had a source. Removed — a fallback that can only
        # ever be empty is noise that invites the wrong root-cause guess.
        # `sector` / `industry` (seeded from the order and the account at
        # checkout) are what actually populate this.
        #
        # Authoritative write: this describes who the vendor is, so it replaces
        # any earlier row rather than adding to a pile the readers then pick from
        # at random.
        sector = (
            (report.assessment_data or {}).get("sector")
            or (report.assessment_data or {}).get("industry")
        )
        if sector:
            from app.services.tender_service import set_vendor_sector
            set_vendor_sector(db, vendor_id, sector, commit=False)

        # Step 2: Create or upsert VendorStatusSnapshot
        snapshot = (
            db.query(VendorStatusSnapshot)
            .filter(VendorStatusSnapshot.vendor_id == vendor_id)
            .first()
        )
        if snapshot:
            if snapshot.verification_depth in ("UNVERIFIED", None):
                snapshot.verification_depth = "BASIC"
            # Reflect actual standing — never silently upgrade NOT_READY → CONDITIONAL.
            snapshot.procurement_readiness = vp_readiness
            snapshot.confidence_score = vp_confidence
            snapshot.computed_at = datetime.now(timezone.utc)
        else:
            snapshot = VendorStatusSnapshot(
                vendor_id=vendor_id,
                verification_depth="BASIC",
                monitoring_activity="ACTIVE",
                risk_signal="CLEAN",
                procurement_readiness=vp_readiness,
                confidence_score=vp_confidence,
                evidence_count=0,
                notarization_depth=0,
                dual_silent_mode="SILENT_RISK_CAPTURE",
            )
            db.add(snapshot)

        # Step 3: Create or upsert VendorScore baseline
        score_row = (
            db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        )
        if score_row:
            if (score_row.compliance_score or 0) < _vp_score_int:
                score_row.compliance_score = _vp_score_int
            if (score_row.total_score or 0) < _vp_score_int:
                score_row.total_score = _vp_score_int
            score_row.updated_at = datetime.now(timezone.utc)
        else:
            score_row = VendorScore(
                vendor_id=vendor_id,
                compliance_score=_vp_score_int,
                total_score=_vp_score_int,
            )
            db.add(score_row)

        # Step 3b: Generate the Vendor Proof certificate PDF → S3 → anchor.
        # Non-fatal: a failure here still leaves the vendor verified; we just
        # don't attach a downloadable certificate.
        cert_url: str | None = None
        cert_tx_hash: str | None = None
        cert_pdf: bytes | None = None
        _vp_expires_display = _vp_expires_at.strftime("%d %B %Y")
        try:
            from app.services.vendor_proof_generator import generate_vendor_proof_certificate
            from app.services.storage import S3Service

            from app.core.models import User as _User
            import hashlib as _hashlib

            # Notarization credit balance at issue — surfaces the redemption line
            # on the certificate. Standalone Vendor Proof grants none; Vendor
            # Trust Pack grants 2 (already applied to the balance by webhook).
            _vp_credits = (
                db.query(_User.notarization_credits)
                .filter(_User.id == vendor_id)
                .scalar()
            ) or 0

            # Sector benchmark — position the Trust Score against same-sector peers
            # (falls back to all-vendors when the sector cohort is too thin, or
            # None when there aren't enough peers to benchmark at all).
            _vp_benchmark = None
            try:
                from app.services.vendor_benchmark import compute_sector_benchmark
                _vp_benchmark = compute_sector_benchmark(
                    db, vendor_id, _pdpa_compliance, sector
                )
            except Exception:
                logger.exception("[VendorProof] sector benchmark computation failed")

            # Score provenance — the driving signal behind each scanned dimension
            # plus an Inferred/Tested basis. Empty when no Deep Scan is on file,
            # in which case the section is simply omitted.
            _vp_score_basis: list = []
            try:
                from app.services.score_basis import build_score_basis
                _vp_score_basis = build_score_basis(db, vendor_id)
            except Exception:
                logger.exception("[VendorProof] score basis computation failed")

            cert_pdf = generate_vendor_proof_certificate(
                company_name=company_name,
                uen=_uen,
                acra_data=acra_info,
                score=(_pdpa_compliance if _pdpa_compliance is not None else "Identity verified only"),
                verification_level="BASIC",
                readiness_label=vp_readiness_label,
                verified_on=datetime.now(timezone.utc).strftime("%d %B %Y"),
                verify_url=verify_url,
                network_name=settings.active_polygon_network_name,
                explorer_url=settings.active_polygon_explorer_url,
                entity_status=acra_info.get("entity_status"),
                expires_on=_vp_expires_display,
                notarization_credits=int(_vp_credits),
                sector_benchmark=_vp_benchmark,
                score_basis=_vp_score_basis,
            )
            cert_hash = _hashlib.sha256(cert_pdf).hexdigest()

            s3 = S3Service()
            cert_report_id = f"vendor-proof-{report_id}"
            cert_url = await s3.upload_pdf(cert_pdf, cert_report_id)

            try:
                from app.services.blockchain import BlockchainService

                # Admin test-checkout reports use a mock tx hash (no gas).
                _demo_anchor = is_demo_anchor(assessment=(report.assessment_data or {}))
                cert_tx_hash = await BlockchainService().anchor_evidence(
                    cert_hash, metadata=f"vendor_proof:{report_id}", demo=_demo_anchor,
                )
            except Exception as _anchor_err:
                logger.warning("[VendorProof] Anchor failed for %s: %s", report_id, _anchor_err)

            report.s3_url = cert_url
            report.file_key = f"reports/{cert_report_id}.pdf"
            report.audit_hash = cert_hash
            if cert_tx_hash:
                report.tx_hash = cert_tx_hash
        except Exception as _cert_err:
            logger.error("[VendorProof] Certificate generation failed for %s: %s", report_id, _cert_err)

        # Mark report complete
        ad = report.assessment_data or {}
        ad["vendor_proof_fulfilled"] = True
        ad["verify_url"] = verify_url
        ad["compliance_score"] = _vp_score_int
        ad["procurement_readiness"] = vp_readiness
        ad["verification_level"] = "BASIC"
        ad["certificate_expires_at"] = _vp_expires_at.isoformat()
        ad["acra_verified"] = acra_info.get("matched", False)
        if acra_info.get("matched"):
            ad["acra_entity_type"] = acra_info.get("entity_type")
            ad["acra_registration_date"] = acra_info.get("registration_date")
            # Backfill the UEN recovered from a name-only live match so the
            # verify page / future refreshes have the official number on file.
            if _uen and not (report.assessment_data or {}).get("uen"):
                ad["uen"] = _uen
        if acra_info.get("entity_status"):
            ad["acra_entity_status"] = acra_info.get("entity_status")
            ad["acra_entity_live"] = acra_info.get("entity_live")
        if cert_url:
            ad["certificate_url"] = cert_url
        if cert_tx_hash:
            ad["certificate_tx_hash"] = cert_tx_hash
        report.assessment_data = ad
        flag_modified(report, "assessment_data")
        report.status = "completed"
        report.completed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            f"[VendorProof] VerifyRecord + snapshot created for vendor {vendor_id}"
        )

        # Step 4: Seed ScoreSnapshot so monitoring shows ACTIVE immediately
        try:
            from app.services.scoring import VendorScoreEngine

            VendorScoreEngine.update_vendor_score(db, str(vendor_id))
        except Exception as e:
            logger.warning(f"[VendorProof] Score update failed for {vendor_id}: {e}")

        # Step 5: Email with embeddable badge
        if contact_email:
            # The badge attests IDENTITY/registration on BOOPPA — not compliance
            # approval. Wording is deliberately "Identity Verified" (not a bare
            # "Verified" that reads as a compliance pass) and the linked verify
            # page shows the real readiness, so a procurement officer is never
            # misled about a vendor with open compliance gaps (audit finding).
            badge_html = (
                f'<a href="{verify_url}" target="_blank" rel="noopener noreferrer" '
                f'style="display:inline-flex;align-items:center;gap:8px;background:#0f172a;'
                f"color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;"
                f'font-family:Arial,sans-serif;font-size:13px;font-weight:600;">'
                f'<span style="color:#10b981;">✓</span> {company_name} — Identity Verified on BOOPPA</a>'
            )
            from app.services.email_templates import get_vendor_proof_activated_html
            body_html = get_vendor_proof_activated_html(
                company_name, vp_score_display, vp_readiness_label, _pdpa_compliance,
                badge_html, _vp_expires_display, cert_url, cert_pdf, report_id,
            )
            try:
                email_svc = EmailService()
                # Deliver the certificate as a direct PDF attachment (not just an
                # expiring S3 link) so it is immediately fileable/forwardable.
                _attachments = None
                if cert_pdf:
                    _safe_co = (company_name or "certificate").replace("/", "-").replace(" ", "-")
                    _attachments = [(f"Vendor-Proof-Certificate-{_safe_co}.pdf", cert_pdf)]
                _ok = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your Vendor Proof is Active — {company_name}",
                    body_html=body_html,
                    attachments=_attachments,
                )
                if not _ok:
                    logger.error("[VendorProof] delivery email rejected for %s", contact_email)
                    await _alert_payment_fulfillment_issue(
                        reason="Vendor Proof activation email rejected by provider",
                        product_type="vendor_proof",
                        customer_email=contact_email,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[VendorProof] Email failed for {contact_email}: {e}")

        logger.info(f"[VendorProof] Fulfilled {report_id} for vendor {vendor_id}")
        _maybe_fire_cover_sheet(contact_email, user_id=vendor_id, test_simulation=is_demo_anchor(assessment=(report.assessment_data or {})))
    except Exception as e:
        # A failure in the core persistence steps (VerifyRecord/snapshot/score/
        # commit) lands here. Previously this only logged and returned, so the
        # buyer paid, no badge email went out, the Celery task saw "success" and
        # never retried, and nobody was alerted — a silent paid-but-unfulfilled
        # Vendor Proof. Surface it and re-raise so `fulfill_vendor_proof_task`
        # retries (transient DB/anchor errors) and support is paged either way.
        logger.error(f"[VendorProof] Fulfillment error for {report_id}: {e}")
        db.rollback()
        try:
            await _alert_payment_fulfillment_issue(
                reason=f"Vendor Proof fulfillment raised before delivery: {e}",
                product_type="vendor_proof",
                customer_email=customer_email,
                extra={"report_id": report_id},
            )
        except Exception as _alert_err:
            logger.error(f"[VendorProof] Alert dispatch failed for {report_id}: {_alert_err}")
        raise
    finally:
        db.close()


async def _deliver_unscannable_and_refund(
    db,
    report,
    *,
    contact_email: str | None,
    session_id: str | None,
    company_name: str | None,
) -> None:
    """Close out a paid scan that can never produce a score: refund, then tell the buyer.

    Incident 2026-07-27 (point-star.com): the scan hit a WAF 403, correctly
    refused to invent findings, and generated an honest "site inaccessible" PDF —
    which fulfillment then threw away. The buyer was charged, received nothing,
    and got a "one small delay, we'll follow up in a few hours" mail that no
    automated path could honour, because this branch returns and never retries.

    So: refund the money, send the buyer the document that already exists, and
    page ops WITHOUT the false reassurance mail.

    Deliberately does NOT write a CertificateLog. There is no score to certify,
    and writing one would hide the report from /admin/stranded-fulfillments.
    """
    import html as _html

    from app.services.refund_service import refund_for_session, refund_summary_for_assessment
    from app.workers.tasks import _set_assessment_values

    # The reason string and company name are attacker-influenced (they come from
    # a remote server's response and the buyer's own input) and go into HTML.
    _html_escape = _html.escape

    assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}
    report_id = str(report.id)

    # Once-only: this whole block re-runs on any re-queue of fulfill_pdpa_task.
    if assessment.get("inaccessible_notice_sent_at"):
        logger.info(f"[PDPA] {report_id} unscannable notice already sent — skipping")
        return

    reason_text = (
        assessment.get("site_inaccessible_reason")
        or "The scanner could not retrieve analysable content from the website."
    )

    # ── Refund ────────────────────────────────────────────────────────────────
    # Sync Stripe call inside async, consistent with the rest of this module
    # (see the Session.modify call in fulfillment/bundles.py).
    refund = refund_for_session(
        session_id,
        reason=f"PDPA scan could not be completed: {report.status}",
    )
    try:
        _set_assessment_values(report, refund_summary_for_assessment(refund))
        flag_modified(report, "assessment_data")
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[PDPA] Could not persist refund state for {report_id}: {e}")

    # ── Tell the buyer, and give them the document we did produce ─────────────
    download_url = f"https://api.booppa.io/api/v1/reports/{report_id}/download"

    if refund.get("refunded"):
        money_line = (
            "We've refunded your payment in full — it should appear on your "
            "statement within 5–10 business days, depending on your bank."
        )
    elif refund.get("skipped_reason") == "demo":
        money_line = "This was an internal test purchase, so no charge was made."
    else:
        money_line = (
            "We're processing a full refund for you. If it hasn't appeared within "
            "a few business days, just reply to this email and we'll sort it out."
        )

    sent = False
    if contact_email:
        try:
            sent = await EmailService().send_html_email(
                to_email=contact_email,
                subject="We couldn't reach your website — full refund issued",
                body_html=branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">We couldn't complete your PDPA scan</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    Our scanner was unable to reach <strong>{_html_escape(company_name or 'your website')}</strong>,
                    so we could not assess it. Rather than issue a score we can't stand behind,
                    we've stopped and refunded you.
                  </p>
                  {email_info_box(f"<strong>What happened:</strong> {_html_escape(reason_text)}")}
                  <p style="margin:16px 0;color:#334155;font-size:15px;line-height:1.6;">
                    {money_line}
                  </p>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    We've still prepared a short report explaining exactly what blocked the scan
                    and how to fix it — most often it's a firewall or CDN rule that needs to allow
                    our scanner through.
                  </p>
                  {email_button(download_url, "Download your report")}
                  <p style="margin:24px 0 0;color:#334155;font-size:15px;line-height:1.6;">
                    Once access is sorted, reply to this email and we'll run the scan again at no charge.
                  </p>
                  <p style="margin:24px 0 0;color:#64748b;font-size:13px;">
                    Order reference: <span style="font-family:monospace;">{session_id or 'n/a'}</span>
                  </p>
                    """,
                    title="Scan could not be completed",
                    preheader="We couldn't reach your site — you've been refunded in full.",
                ),
            )
            # send_html_email returns False on provider rejection; it does not raise.
            if not sent:
                logger.error(
                    f"[PDPA] Unscannable-notice email REJECTED by provider for {report_id} "
                    f"(to={contact_email})"
                )
        except Exception as e:
            logger.error(f"[PDPA] Unscannable-notice email failed for {report_id}: {e}")
    else:
        logger.error(f"[PDPA] No contact email for {report_id} — cannot notify buyer")

    # Only claim the notice as sent if it actually went out, so a retry can
    # re-attempt delivery rather than the buyer being silently skipped.
    if sent:
        try:
            _set_assessment_values(
                report,
                {"inaccessible_notice_sent_at": datetime.now(timezone.utc).isoformat()},
            )
            flag_modified(report, "assessment_data")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"[PDPA] Could not record notice timestamp for {report_id}: {e}")

    # ── Page ops, but suppress the misleading customer mail ───────────────────
    # notify_customer=False: the buyer has just had an accurate email. The
    # generic "one small delay … we will follow up within a few hours" notice
    # would contradict it and promise a follow-up that cannot happen.
    await _alert_payment_fulfillment_issue(
        reason=(
            f"PDPA scan ended in terminal state '{report.status}' — no score can be "
            f"produced. Buyer notified and refund {'issued' if refund.get('refunded') else 'NOT issued'}."
        ),
        product_type="pdpa_quick_scan",
        customer_email=contact_email,
        session_id=session_id,
        extra={
            "report_id": report_id,
            "report_status": report.status,
            "refunded": bool(refund.get("refunded")),
            "refund_id": refund.get("refund_id"),
            "refund_skipped_reason": refund.get("skipped_reason"),
            "buyer_notified": bool(sent),
        },
        notify_customer=False,
    )


async def _fulfill_pdpa(report_id: str, customer_email: str | None, send_email: bool = True, raise_if_incomplete: bool = False) -> None:
    """
    PDPA Snapshot fulfillment:
    1. Run the full on-page + AI scan (if not already done)
    2. Generate branded PDF report
    3. Upload to S3
    4. Update vendor compliance score (+8 to +25 pts)
    5. Write CertificateLog entry
    6. Send email with PDF download link

    `send_email=False` suppresses the standalone "Snapshot Ready" email. The
    PDPA Monitor / Vendor Pro cycle uses this so the buyer gets a SINGLE
    consolidated PDPA deliverable email (the month-over-month Monitor report)
    instead of a raw Quick-Scan email plus the Monitor report — see
    `pdpa_monitor_monthly_rescan_task`. The score/PDF/CertificateLog side
    effects still run; only the email is gated.
    """
    db = SessionLocal()
    session_id = None
    contact_email = customer_email
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"[PDPA] Report {report_id} not found")
            return

        assessment = (
            report.assessment_data if isinstance(report.assessment_data, dict) else {}
        )
        # Read early so the tail handler can always attribute an alert to a
        # Stripe session — alerts from this path used to render "(missing)"
        # even though _create_stub_report persists the session id right here.
        session_id = assessment.get("stripe_session_id")
        contact_email = (
            customer_email
            or assessment.get("contact_email")
            or assessment.get("customer_email")
        )
        from app.services.evidence_enricher import resolve_report_legal_name
        company_name = await resolve_report_legal_name(report, db) or (
            report.company_name or "Customer"
        )
        # Resolve the URL the same way process_report_task does (tasks.py) so the
        # "ASSESSED URL" line in the PDF shows the URL that was actually scanned
        # (post-redirect), not whatever bare domain the buyer typed.
        website_url = (
            assessment.get("resolved_url")
            or assessment.get("url")
            or report.company_website
            or assessment.get("website", "")
        )

        # ── Step 0: Already fulfilled? ─────────────────────────────────────
        # The scan→fulfill chain, the webhook branch and a manual re-queue can
        # each land here for the same report. Without this guard every pass
        # inserts a duplicate CertificateLog and re-sends the buyer's snapshot
        # email (the scan path dedupes its own mail via
        # `_send_report_ready_email_once`; this one had no equivalent).
        if assessment.get("pdf_generated"):
            from app.core.models import CertificateLog

            already = (
                db.query(CertificateLog)
                .filter(
                    CertificateLog.report_id == report.id,
                    CertificateLog.certificate_type == "PDPA",
                )
                .first()
            )
            if already:
                logger.info(f"[PDPA] {report_id} already fulfilled — skipping")
                return

        # ── Step 1: Ensure scan is complete ────────────────────────────────
        # If the report already has a risk score from a prior scan, use it.
        # Otherwise chain the generic processing task. Read the score through
        # the canonical resolver: process_report_task persists it nested under
        # `booppa_report.risk_assessment.score`, so a top-level-only read is
        # never satisfied and the report strands (paid, undelivered).
        from app.services.pdpa_findings import resolve_pdpa_risk_score

        risk_score = resolve_pdpa_risk_score(assessment)
        if risk_score is None:
            # A scan that ended in one of these states cannot ever produce a
            # score — rescanning a 403 site or a blocked report just burns AI
            # spend and ends in a silent MaxRetriesExceeded. Stop, refund, and
            # deliver what we do have.
            #
            # `site_inaccessible` is emphatically NOT "already fulfilled by the
            # workflow": process_report_task generates and uploads the
            # inaccessible PDF, then returns before ever sending it (see the
            # gate in tasks.py). Nothing reaches the buyer unless this branch
            # sends it.
            if (
                report.status in ("site_inaccessible", "blocked", "failed")
                or assessment.get("site_inaccessible")
            ):
                logger.error(
                    f"[PDPA] {report_id} terminal status={report.status}, cannot fulfil"
                )
                await _deliver_unscannable_and_refund(
                    db,
                    report,
                    contact_email=contact_email,
                    session_id=session_id,
                    company_name=company_name,
                )
                return
            # Scan not yet run. Rather than raise and lean on blind exponential
            # backoff (which pushed the confirmation email hours out — or never,
            # if no worker drained the retry), chain fulfillment to the scan so
            # it re-runs the moment `risk_score` is written. The `link` fires only
            # on successful completion of process_report_task.
            already_chained = bool(assessment.get("_pdpa_fulfill_chained"))
            try:
                from app.workers.tasks import process_report_task, fulfill_pdpa_task

                if not already_chained:
                    # One-shot flag so a re-entry can't chain endlessly.
                    assessment["_pdpa_fulfill_chained"] = True
                    report.assessment_data = assessment
                    from sqlalchemy.orm.attributes import flag_modified as _fm

                    _fm(report, "assessment_data")
                    db.commit()
                    process_report_task.apply_async(
                        args=[str(report.id)],
                        link=fulfill_pdpa_task.si(str(report.id), customer_email, send_email),
                    )
                    logger.info(
                        f"[PDPA] Chained scan→fulfillment for {report_id} (risk_score missing)"
                    )
                    return
                # Scan already ran once and the score is still missing. Do NOT
                # queue another scan — a retry loop that re-runs the full AI scan
                # + anchoring on every attempt is how one stranded report cost us
                # ~11 scans. Fall through to the bounded task-retry safety net,
                # which now alerts loudly when it gives up.
                logger.warning(
                    f"[PDPA] Score still missing for {report_id} after chained scan"
                )
            except Exception as e:
                logger.error(f"[PDPA] Could not chain scan for {report_id}: {e}")

            if raise_if_incomplete:
                # Fallback: raise so fulfill_pdpa_task retries and eventually writes
                # the Compliance score and CertificateLog when the scan completes.
                raise Exception("PDPA scan not yet complete, retrying later")
            return

        # ── Step 2: Generate PDF ────────────────────────────────────────────
        pdf_bytes = None
        try:
            pdf_service = PDFService()
            # The AI's structured report lives nested under `booppa_report`
            # (process_report_task persists it there). Reading `detailed_findings`
            # off the top level of assessment_data silently yielded [] on every
            # run, and generate_pdf's PDPA branch treats an empty finding list as
            # "no violations" — printing "found no PDPA violations" above a score
            # table showing Non-Compliant dimensions.
            #
            # Resolve via the shared helper rather than a local fallback chain:
            # a hand-rolled copy here missed `risk_assessment.findings`, the
            # legacy `violations` key, and the dict->list coercion, so older
            # reports would still have resolved to [] and reprinted the same
            # contradiction. One resolver = one answer for every consumer.
            from app.services.pdpa_findings import resolve_pdpa_findings

            _structured = assessment.get("booppa_report") or {}
            _findings = resolve_pdpa_findings(assessment)
            pdf_data = {
                "report_id": report_id,
                "framework": report.framework or "pdpa_quick_scan",
                # Carry the scan's assessment_data so the PDF can brand itself by
                # trigger source (assessment_data["triggered_by"]) — e.g. a PDPA
                # Monitor rescan renders "PDPA Monitor" instead of a plain Quick
                # Scan. Without this key the branding block in generate_pdf can
                # never fire.
                "assessment_data": assessment,
                "company_name": company_name,
                # `company_url` is not a key the PDF reads — the "ASSESSED URL"
                # row looks at vendor_url/website_url/url. Kept for any other
                # consumer, but `website_url` is what renders.
                "company_url": website_url,
                "website_url": website_url,
                "created_at": (
                    report.created_at.isoformat()
                    if report.created_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                "status": "completed",
                "risk_score": risk_score,
                "risk_level": assessment.get("risk_level")
                or (_structured.get("risk_assessment") or {}).get("level")
                or assessment.get("risk_assessment", {}).get("level", "MEDIUM"),
                "findings": _findings,
                "summary": _structured.get("executive_summary")
                or assessment.get("executive_summary", ""),
                # Pass structured report sections so PDF renders full findings +
                # recommendations. The PDPA branch of generate_pdf reads ONLY
                # `structured_report`; the top-level copies below serve the
                # non-PDPA renderers.
                "structured_report": _structured,
                "executive_summary": _structured.get("executive_summary")
                or assessment.get("executive_summary", ""),
                "detailed_findings": _findings,
                "recommendations": _structured.get("recommendations")
                or assessment.get("recommendations", []),
                "legal_references": _structured.get("legal_references")
                or assessment.get("legal_references", []),
                # Carry the anchor recorded by process_report_task so the rebuilt
                # PDF keeps it. is_real_onchain_tx still gates display, so a
                # test-checkout demo hash correctly renders "—".
                "tx_hash": report.tx_hash,
                "audit_hash": report.audit_hash,
                "risk_assessment": _structured.get("risk_assessment")
                or assessment.get("risk_assessment", {}),
                # Screenshot — prefer stored base64, fallback to live capture
                "site_screenshot": assessment.get("site_screenshot")
                or assessment.get("screenshot"),
                "payment_confirmed": True,
                "tier": assessment.get("tier", "pro"),
                "contact_email": contact_email,
                "base_url": "https://www.booppa.io",
            }
            # Pass raw scan evidence so the dimension-weighted score this PDF
            # computes matches the canonical scan report (process_report_task).
            # Without these, _compliance_score_table scores from findings alone
            # and can diverge from the rich report — the 53-vs-54 bug.
            for _scan_key in (
                "security_headers", "consent_mechanism", "privacy_policy",
                "dpo_compliance", "dnc_mention", "nric_evidence", "nric",
                "policy_clauses", "pdpc_enforcement", "hosting", "trackers",
                "ssl_grade", "primary_language",
            ):
                if _scan_key in assessment:
                    pdf_data[_scan_key] = assessment[_scan_key]
            # Capture screenshot live if not already stored
            if not pdf_data["site_screenshot"] and website_url:
                try:
                    from app.services.screenshot_service import (
                        capture_screenshot_base64,
                    )

                    import asyncio
                    ss = await asyncio.to_thread(capture_screenshot_base64, website_url)
                    if ss:
                        pdf_data["site_screenshot"] = ss
                        assessment["site_screenshot"] = ss
                        flag_modified(report, "assessment_data")
                        db.commit()
                except Exception as ss_err:
                    logger.warning(
                        f"[PDPA] Screenshot capture failed for {report_id}: {ss_err}"
                    )

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
        report.completed_at = datetime.now(timezone.utc)
        assessment["pdf_generated"] = True
        assessment["pdf_url"] = pdf_url
        assessment["on_page_only"] = False
        # Persist the EXACT headline compliance score this PDF printed (the
        # dimension-weighted overall, stashed by _compliance_score_table) and
        # the exact URL it displayed, so the Compliance Evidence Cover Sheet
        # reproduces both verbatim instead of recomputing and drifting (the
        # 53-vs-54 / crayon.com-vs-crayon.com/sg inconsistency in the audit).
        _computed_score = pdf_data.get("computed_overall_compliance_score")
        if _computed_score is None and isinstance(pdf_data.get("scan_data"), dict):
            # Defensive: _compliance_score_table stashes onto report_data["scan_data"]
            # when that nested key is present, so read it back from there too.
            _computed_score = pdf_data["scan_data"].get("computed_overall_compliance_score")
        if _computed_score is not None:
            assessment["compliance_score"] = _computed_score
        if website_url:
            assessment["display_url"] = website_url
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
            from app.core.models import CertificateLog

            cert = CertificateLog(
                vendor_id=report.owner_id,
                certificate_type="PDPA",
                report_id=report.id,
                file_key=report.file_key,
                generated_at=datetime.now(timezone.utc),
            )
            db.add(cert)
            db.commit()
        except Exception as e:
            logger.error(f"[PDPA] CertificateLog failed for {report_id}: {e}")

        # ── Step 6: Email PDF to vendor ────────────────────────────────────
        # Gated on send_email so the PDPA Monitor / Vendor Pro cycle can deliver
        # ONE consolidated PDPA email (the Monitor report) instead of two.
        if contact_email and send_email:
            try:
                download_section = (
                    email_button(pdf_url, "Download PDPA Snapshot Report (PDF)")
                    if pdf_url
                    else '<p style="margin:0 0 16px;color:#334155;font-size:15px;">Your report will be available on the BOOPPA dashboard shortly.</p>'
                )
                # Show the SAME compliance score the PDF + Cover Sheet show
                # (dimension-weighted, persisted), not a separate 100-risk figure
                # that drifted (e.g. 54 in the email vs 53 in the PDF).
                _email_compliance = assessment.get("compliance_score")
                if not isinstance(_email_compliance, (int, float)):
                    from app.services.pdpa_findings import resolve_pdpa_score
                    _email_compliance = resolve_pdpa_score(assessment) or 0
                _email_compliance = int(_email_compliance)
                from app.services.email_templates import get_pdpa_snapshot_ready_html
                body_html = get_pdpa_snapshot_ready_html(
                    company_name, website_url, _email_compliance, report_id, download_section,
                )
                email_svc = EmailService()
                _attachments = None
                if pdf_bytes:
                    _safe_co = (company_name or "report").replace("/", "-").replace(" ", "-")
                    _attachments = [(f"PDPA_Snapshot_{_safe_co}.pdf", pdf_bytes)]
                sent = await email_svc.send_html_email(
                    to_email=contact_email,
                    subject=f"Your PDPA Snapshot Report is Ready — {company_name}",
                    body_html=body_html,
                    attachments=_attachments,
                )
                if not sent:
                    await _alert_payment_fulfillment_issue(
                        reason="PDPA snapshot report email rejected by provider",
                        product_type="pdpa_quick_scan",
                        customer_email=contact_email,
                        session_id=session_id,
                        extra={"report_id": report_id},
                    )
            except Exception as e:
                logger.error(f"[PDPA] Email failed for {contact_email}: {e}")

        logger.info(
            f"[PDPA] Fulfilled {report_id} for vendor {vendor_id} pdf={pdf_url}"
        )
        _maybe_fire_cover_sheet(contact_email, user_id=vendor_id, test_simulation=is_demo_anchor(assessment=(report.assessment_data or {})))
    except Exception as e:
        logger.error(f"[PDPA] Fulfillment error for {report_id}: {e}")
        db.rollback()
        await _alert_payment_fulfillment_issue(
            reason=f"PDPA fulfillment raised exception: {type(e).__name__}: {e}",
            product_type="pdpa_quick_scan",
            customer_email=contact_email,
            session_id=session_id,
            extra={"report_id": report_id},
        )
        raise
    finally:
        db.close()


