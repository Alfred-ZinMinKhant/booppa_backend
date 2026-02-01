from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from app.core.db import get_db, get_current_user, SessionLocal
from app.core.config import settings
import stripe
import logging
from app.core.models import Report, User
from app.workers.tasks import process_report_task
from sqlalchemy.orm import Session
import uuid
import asyncio

from app.workers.tasks import process_report_workflow

router = APIRouter()

logger = logging.getLogger(__name__)


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
        if getattr(request, "contact_email", None):
            assessment["contact_email"] = request.contact_email

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

        # Start background processing
        background_tasks.add_task(process_report_task, str(report.id))

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
        if getattr(request, "contact_email", None):
            assessment["contact_email"] = request.contact_email

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


@router.get("/by-session")
async def get_report_by_session(
    session_id: str | None = None,
    debug: bool = False,
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
        try:
            payment_status = session.get("payment_status")
            if payment_status == "paid":
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["payment_confirmed"] = True
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
        try:
            if isinstance(report.assessment_data, dict):
                structured_report = report.assessment_data.get("booppa_report")
            site_screenshot = report.assessment_data.get("site_screenshot")
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
        else:
            # If report isn't ready, try to kick off processing on demand.
            try:
                if report.status != "processing":
                    report.status = "processing"
                    db.commit()

                # Prefer celery if available, otherwise run workflow in background.
                try:
                    process_report_task.delay(str(report.id))
                except Exception:
                    if background_tasks is not None:
                        background_tasks.add_task(
                            _run_report_workflow_sync, str(report.id)
                        )
            except Exception as e:
                logger.warning(
                    f"Failed to trigger on-demand processing for {report_id}: {e}"
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

    return report
