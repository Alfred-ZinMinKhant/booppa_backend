from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Response
from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from app.core.db import get_db, get_current_user, SessionLocal
from app.core.config import settings
import stripe
import logging
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
import base64
from io import BytesIO
import qrcode
from app.workers.tasks import process_report_workflow
from app.billing.enforcement import enforce_tier
from sqlalchemy.orm import Session
import uuid
import asyncio
from datetime import datetime


router = APIRouter()

logger = logging.getLogger(__name__)

MAX_PROCESSING_ATTEMPTS = 3
PROCESSING_RETRY_WINDOW_SECONDS = 600


class ReportRequest(BaseModel):
    framework: str
    company_name: str
    website: Optional[str] = None
    contact_email: Optional[str] = None
    assessment_data: dict


class ReportResponse(BaseModel):
    id: UUID
    status: str
    framework: str
    company_name: str
    company_website: Optional[str] = None
    created_at: str


def _build_verify_payload(report: Report) -> dict:
    audit_hash = report.audit_hash
    if not audit_hash:
        return {}

    verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/{audit_hash}"
    tx_hash = report.tx_hash
    anchored = False
    anchored_at = None
    tx_confirmed = None

    if tx_hash:
        blockchain = BlockchainService()
        status = blockchain.get_anchor_status(audit_hash, tx_hash=tx_hash)
        anchored = status.get("anchored", False)
        anchored_at = status.get("anchored_at")
        tx_confirmed = status.get("tx_confirmed")

    qr_image = _build_qr_image(verify_url) if verify_url else None

    return {
        "qr_target": verify_url,
        "qr_image": qr_image,
        "proof_header": "BOOPPA-PROOF-SG",
        "schema_version": "1.0",
        "verify_url": verify_url,
        "tx_hash": tx_hash,
        "anchored": anchored,
        "anchored_at": anchored_at,
        "tx_confirmed": tx_confirmed,
    }


def _build_qr_image(target: str) -> str | None:
    if not target:
        return None


def _build_qr_png(target: str) -> bytes | None:
    if not target:
        return None
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(target)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        logger.warning("QR PNG generation failed: %s", exc)
        return None
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(target)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:
        logger.warning("QR generation failed: %s", exc)
        return None


@router.post("", response_model=ReportResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_report(
    request: ReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new audit report and start background processing"""
    try:
        # Create report in database
        # Ensure assessment_data includes URL/website for scanning
        assessment = request.assessment_data or {}
        if request.website:
            assessment["url"] = request.website
            if "uses_https" not in assessment and isinstance(request.website, str):
                assessment["uses_https"] = request.website.lower().startswith("https://")
        if getattr(request, "contact_email", None):
            assessment["contact_email"] = request.contact_email

        if request.framework in {"pdpa_free_scan"}:
            assessment["on_page_only"] = True
            assessment["tier"] = "free"

        report = Report(
            owner_id=current_user.id,
            framework=request.framework,
            company_name=request.company_name,
            company_website=request.website,
            assessment_data=assessment,
            status="processing",
        )

        db.add(report)
        db.commit()
        db.refresh(report)

        # Start background processing (synchronous workflow, no Celery/Redis)
        background_tasks.add_task(_run_report_workflow_sync, str(report.id))

        return ReportResponse(
            id=report.id,
            status=report.status,
            framework=report.framework,
            company_name=report.company_name,
            created_at=report.created_at.isoformat(),
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create report: {str(e)}",
        )


@router.post("/public", status_code=status.HTTP_201_CREATED)
async def create_report_public(request: ReportRequest):
    """Create a minimal report without authentication for use before checkout.
    Returns `{ "report_id": "<uuid>" }`.
    """
    db = SessionLocal()
    try:
        assessment = request.assessment_data or {}
        if request.website:
            assessment["url"] = request.website
            if "uses_https" not in assessment and isinstance(request.website, str):
                assessment["uses_https"] = request.website.lower().startswith("https://")
        if getattr(request, "contact_email", None):
            assessment["contact_email"] = request.contact_email

        if request.framework in {"pdpa_free_scan"}:
            assessment["on_page_only"] = True
            assessment["tier"] = "free"

        report = Report(
            owner_id=str(uuid.uuid4()),
            framework=request.framework,
            company_name=request.company_name,
            company_website=request.website,
            assessment_data=assessment,
            status="pending",
        )
        db.add(report)
        db.commit()
        db.refresh(report)

        return {"report_id": str(report.id)}
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"Failed to create public report: {e}"
        )
    finally:
        db.close()


def _run_report_workflow_sync(report_id: str) -> None:
    try:
        asyncio.run(process_report_workflow(report_id))
    except Exception as exc:
        logger.error(f"On-demand report processing failed for {report_id}: {exc}")
        db = SessionLocal()
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if report:
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["last_processing_error"] = str(exc)[:500]
                report.assessment_data = assessment
                report.status = "failed"
                db.commit()
        finally:
            db.close()


async def _run_report_workflow_async(report_id: str) -> None:
    try:
        await process_report_workflow(report_id)
    except Exception as exc:
        logger.error(f"On-demand async processing failed for {report_id}: {exc}")
        db = SessionLocal()
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if report:
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["last_processing_error"] = str(exc)[:500]
                report.assessment_data = assessment
                report.status = "failed"
                db.commit()
        finally:
            db.close()


@router.get("/by-session")
async def get_report_by_session(
    session_id: str | None = None,
    debug: bool = False,
    force: bool = False,
    background_tasks: BackgroundTasks = None,
):
    """Public endpoint: lookup a report by a Stripe Checkout `session_id`.
    The Stripe session should contain `metadata.report_id` or `client_reference_id`.
    Returns JSON `{ url: <presigned_s3_url> }` when the report PDF is available.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")

    stripe_key = settings.STRIPE_SECRET_KEY
    if not stripe_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    stripe.api_key = stripe_key
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        logger.error(f"Failed to retrieve Stripe session {session_id}: {e}")
        raise HTTPException(status_code=400, detail="Invalid session_id")

    metadata = session.get("metadata") or {}
    logger.info(
        f"Stripe session metadata: {metadata}, client_reference_id={session.get('client_reference_id')}"
    )
    report_id = (
        metadata.get("report_id")
        or metadata.get("reportId")
        or session.get("client_reference_id")
    )

    if not report_id:
        raise HTTPException(
            status_code=404, detail="No report mapping found for session"
        )

    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # If payment succeeded, mark it on the report so downstream processing can anchor evidence.
        payment_status = session.get("payment_status")
        session_status = session.get("status")
        session_paid = payment_status == "paid" or session_status == "complete"
        try:
            assessment = report.assessment_data or {}
            if not isinstance(assessment, dict):
                assessment = {}
            if session_paid and not assessment.get("payment_confirmed"):
                assessment["payment_confirmed"] = True
                assessment["payment_confirmed_at"] = datetime.utcnow().isoformat()
                assessment["payment_source"] = "stripe_session"
            product_type = metadata.get("product_type")
            if product_type:
                assessment["product_type"] = product_type
            customer_email = metadata.get("customer_email") or session.get(
                "customer_details", {}
            ).get("email")
            if customer_email:
                assessment["contact_email"] = customer_email
            report.assessment_data = assessment
            db.commit()
        except Exception as e:
            logger.warning(f"Failed to update payment status for {report_id}: {e}")

        structured_report = None
        site_screenshot = None
        last_processing_error = None
        last_processing_attempt_at = None
        processing_attempts = None
        url_resolution_error = None
        resolved_url = None
        uses_https = None
        http_status = None
        screenshot_error = None
        screenshot_url = None
        workflow_flags = {}
        try:
            if isinstance(report.assessment_data, dict):
                structured_report = report.assessment_data.get("booppa_report")
                site_screenshot = report.assessment_data.get("site_screenshot")
                url_resolution_error = report.assessment_data.get("url_resolution_error")
                resolved_url = report.assessment_data.get("resolved_url")
                uses_https = report.assessment_data.get("uses_https")
                http_status = report.assessment_data.get("http_status")
                screenshot_error = report.assessment_data.get("screenshot_error")
                screenshot_url = report.assessment_data.get("screenshot_url")
                last_processing_error = report.assessment_data.get(
                    "last_processing_error"
                )
                last_processing_attempt_at = report.assessment_data.get(
                    "last_processing_attempt_at"
                )
                processing_attempts = report.assessment_data.get(
                    "processing_attempts"
                )
                workflow_flags = {
                    "booppa_report_saved_at": report.assessment_data.get(
                        "booppa_report_saved_at"
                    ),
                    "pdf_generated": report.assessment_data.get("pdf_generated"),
                    "pdf_generated_at": report.assessment_data.get("pdf_generated_at"),
                    "s3_uploaded": report.assessment_data.get("s3_uploaded"),
                    "s3_uploaded_at": report.assessment_data.get("s3_uploaded_at"),
                }
        except Exception:
            structured_report = None

        if not structured_report and report.ai_narrative:
            structured_report = {
                "executive_summary": report.ai_narrative,
                "report_metadata": {"report_id": str(report.id)},
            }

        policy = enforce_tier(report.assessment_data, report.framework)
        features = policy.get("features", {}) if isinstance(policy, dict) else {}
        if session_paid and policy.get("tier") in {"PRO", "ENTERPRISE"}:
            features = {
                **features,
                "ai_mode": "full",
                "ai_full": True,
                "pdf": True,
                "blockchain": True,
            }
            try:
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["tier"] = policy.get("tier")
                assessment["tier_features"] = features
                report.assessment_data = assessment
                db.commit()
            except Exception:
                db.rollback()
        needs_paid_output = (
            (policy.get("paid") or session_paid)
            and (features.get("ai_full") or features.get("pdf"))
            and (
                not report.assessment_data
                or not isinstance(report.assessment_data, dict)
                or not report.assessment_data.get("booppa_report_saved_at")
                or report.assessment_data.get("pdf_generated") is False
            )
        )

        if not needs_paid_output and (report.s3_url or structured_report):
            verify_payload = _build_verify_payload(report)
            return {
                "status": report.status,
                "url": report.s3_url,
                "report": structured_report,
                "report_id": str(report.id),
                "framework": report.framework,
                "payment_confirmed": bool(
                    report.assessment_data.get("payment_confirmed")
                    if isinstance(report.assessment_data, dict)
                    else False
                ),
                "stripe_payment_status": payment_status,
                "stripe_session_status": session_status,
                "tier": policy.get("tier"),
                "tier_features": features,
                "verification": verify_payload,
                "site_screenshot": site_screenshot,
                "resolved_url": resolved_url,
                "uses_https": uses_https,
                "http_status": http_status,
                "url_resolution_error": url_resolution_error,
                "screenshot_error": screenshot_error,
                "screenshot_url": screenshot_url,
                "workflow": workflow_flags,
            }
        else:
            # If report isn't ready, try to kick off processing on demand.
            try:
                # Track processing attempts for debugging.
                try:
                    now = datetime.utcnow()
                    assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}
                    attempts = assessment.get("processing_attempts")
                    try:
                        attempts = int(attempts) if attempts is not None else 0
                    except Exception:
                        attempts = 0
                    last_attempt = assessment.get("last_processing_attempt_at")
                    last_attempt_dt = None
                    if isinstance(last_attempt, str):
                        try:
                            last_attempt_dt = datetime.fromisoformat(last_attempt)
                        except Exception:
                            last_attempt_dt = None

                    if attempts >= MAX_PROCESSING_ATTEMPTS:
                        raise HTTPException(
                            status_code=429,
                            detail="Processing retry limit reached. Please try again later.",
                        )

                    if last_attempt_dt and (now - last_attempt_dt).total_seconds() < PROCESSING_RETRY_WINDOW_SECONDS:
                        raise HTTPException(
                            status_code=429,
                            detail="Processing retry already triggered. Please wait before retrying.",
                        )

                    assessment["processing_attempts"] = attempts + 1
                    assessment["last_processing_attempt_at"] = now.isoformat()
                    report.assessment_data = assessment
                    db.commit()
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(
                        f"Failed to update processing attempt metadata for {report_id}: {e}"
                    )

                if report.status != "processing":
                    report.status = "processing"
                    db.commit()

                if background_tasks is not None:
                    background_tasks.add_task(
                        _run_report_workflow_sync, str(report.id)
                    )
                else:
                    # If background tasks are unavailable, schedule a single async run.
                    try:
                        asyncio.create_task(_run_report_workflow_async(str(report.id)))
                    except Exception as e:
                        logger.warning(
                            f"Failed to schedule async processing for {report_id}: {e}"
                        )
            except Exception as e:
                logger.warning(
                    f"Failed to trigger on-demand processing for {report_id}: {e}"
                )

            if force:
                try:
                    await process_report_workflow(str(report.id))
                    db.refresh(report)
                    structured_report = None
                    try:
                        if isinstance(report.assessment_data, dict):
                            structured_report = report.assessment_data.get(
                                "booppa_report"
                            )
                    except Exception:
                        structured_report = None

                    if report.s3_url or structured_report:
                        return {
                            "status": report.status,
                            "url": report.s3_url,
                            "report": structured_report,
                            "report_id": str(report.id),
                            "site_screenshot": site_screenshot,
                        }
                except Exception as e:
                    logger.error(
                        f"Force processing failed for {report_id}: {e}"
                    )
            if debug:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": "Report not ready",
                        "report_id": str(report.id),
                        "status": report.status,
                        "has_pdf": bool(report.s3_url),
                        "has_report": bool(structured_report),
                        "payment_status": session.get("payment_status"),
                        "last_processing_error": last_processing_error,
                        "processing_attempts": processing_attempts,
                        "last_processing_attempt_at": last_processing_attempt_at,
                        "workflow": workflow_flags,
                    },
                )
            raise HTTPException(status_code=404, detail="Report not ready")
    finally:
        db.close()


@router.get("/{report_id}")
async def get_report(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get report status and details"""
    report = (
        db.query(Report)
        .filter(Report.id == report_id, Report.owner_id == current_user.id)
        .first()
    )

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
        )

    structured_report = None
    if isinstance(report.assessment_data, dict):
        structured_report = report.assessment_data.get("booppa_report")

    if not structured_report and report.ai_narrative:
        structured_report = {
            "executive_summary": report.ai_narrative,
            "report_metadata": {"report_id": str(report.id)},
        }

    return {
        "status": report.status,
        "url": report.s3_url,
        "report": structured_report,
        "report_id": str(report.id),
        "framework": report.framework,
        "payment_confirmed": bool(
            report.assessment_data.get("payment_confirmed")
            if isinstance(report.assessment_data, dict)
            else False
        ),
        "tier": report.assessment_data.get("tier")
        if isinstance(report.assessment_data, dict)
        else None,
        "tier_features": report.assessment_data.get("tier_features")
        if isinstance(report.assessment_data, dict)
        else None,
        "verification": _build_verify_payload(report),
    }


@router.get("/{report_id}/qr")
async def get_report_qr(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a QR code PNG for the report verification URL (read-only)."""
    report = (
        db.query(Report)
        .filter(Report.id == report_id, Report.owner_id == current_user.id)
        .first()
    )

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
        )

    if not report.audit_hash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verification hash not available",
        )

    verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/{report.audit_hash}"
    png_bytes = _build_qr_png(verify_url)
    if not png_bytes:
        raise HTTPException(status_code=500, detail="QR generation failed")

    return Response(content=png_bytes, media_type="image/png")
