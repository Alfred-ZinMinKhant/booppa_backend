from datetime import datetime, timedelta
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr

from app.core.db import SessionLocal
from app.core.models import Report
from app.services.pdf_service import PDFService
from app.services.screenshot_service import capture_screenshot_base64
from app.orchestrator.engine import run
from sqlalchemy import and_

logger = logging.getLogger(__name__)

router = APIRouter()


class QRScanRequest(BaseModel):
    website_url: str
    company_name: str | None = None
    email: EmailStr


@router.post("/qr-scan")
async def qr_scan(payload: QRScanRequest):
    """Run the free PDPA scan and return a PDF report."""
    db = SessionLocal()
    report_row = None
    try:
        website_url = payload.website_url.strip()
        uses_https = website_url.lower().startswith("https://")
        month_ago = datetime.utcnow() - timedelta(days=30)

        existing = (
            db.query(Report)
            .filter(
                and_(
                    Report.framework == "pdpa_free_scan",
                    Report.created_at >= month_ago,
                    Report.assessment_data["contact_email"].astext == payload.email,
                )
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=429,
                detail="Free scan is limited to once per month per email.",
            )

        scan_data = {
            "company_name": payload.company_name or "Free PDPA Scan",
            "url": website_url,
            "scan_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "uses_https": uses_https,
            "assessment_source": "free_scan",
            "contact_email": payload.email,
        }

        report_row = Report(
            owner_id=str(uuid.uuid4()),
            framework="pdpa_free_scan",
            company_name=scan_data.get("company_name"),
            company_website=website_url,
            assessment_data=scan_data,
            status="processing",
        )
        db.add(report_row)
        db.commit()
        db.refresh(report_row)

        orchestrated = await run(website_url)
        ai_report = orchestrated.get("ai") if isinstance(orchestrated, dict) else None
        if not isinstance(ai_report, dict):
            ai_report = {}

        if isinstance(orchestrated, dict):
            scan_data["orchestrator_scan"] = orchestrated.get("scan")
            scan_data["orchestrator_notary_hash"] = orchestrated.get("notary_hash")
            scan_data["orchestrator_blockchain_tx_hash"] = orchestrated.get(
                "blockchain_tx_hash"
            )
            try:
                screenshot_url = scan_data.get("url") or website_url
                if isinstance(screenshot_url, str) and screenshot_url:
                    screenshot_b64 = await asyncio.wait_for(
                        asyncio.to_thread(capture_screenshot_base64, screenshot_url),
                        timeout=25,
                    )
                    if screenshot_b64:
                        scan_data["site_screenshot"] = screenshot_b64
                    else:
                        scan_data["screenshot_error"] = "capture_failed_or_timeout"
                        scan_data["screenshot_url"] = screenshot_url
            except Exception:
                scan_data["screenshot_error"] = "capture_failed_or_timeout"
                scan_data["screenshot_url"] = scan_data.get("url") or website_url

            try:
                report_row.assessment_data = scan_data
                db.commit()
            except Exception:
                db.rollback()

        report_id = f"FREE-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        pdf_payload = {
            "report_id": report_id,
            "framework": "PDPA Quick Scan (Free)",
            "company_name": scan_data.get("company_name"),
            "created_at": datetime.utcnow().isoformat(),
            "status": "completed",
            "tx_hash": None,
            "audit_hash": None,
            "ai_narrative": ai_report.get("executive_summary")
            or ai_report.get("summary", ""),
            "structured_report": ai_report,
            "payment_confirmed": False,
            "contact_email": payload.email,
            "key_issues": [
                f"{f.get('severity', 'MEDIUM')}: {f.get('type', '').replace('_', ' ').title()}"
                for f in ai_report.get("detailed_findings", [])[:5]
            ],
        }

        pdf_service = PDFService()
        pdf_bytes = pdf_service.generate_pdf(pdf_payload)

        try:
            report_row.status = "completed"
            db.commit()
        except Exception:
            db.rollback()

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="Booppa-PDPA-Scan.pdf"'
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        if report_row:
            try:
                report_row.status = "failed"
                db.commit()
            except Exception:
                db.rollback()
        logger.error(f"Free scan failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate scan report")
    finally:
        db.close()
