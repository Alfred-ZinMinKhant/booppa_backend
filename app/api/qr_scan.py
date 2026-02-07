from datetime import datetime, timedelta
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.core.db import SessionLocal
from app.core.models import Report
from app.services.screenshot_service import capture_screenshot_base64
from app.integrations.scan1.adapter import run_scan_async
from app.integrations.ai.adapter import ai_light
from sqlalchemy import and_

logger = logging.getLogger(__name__)

router = APIRouter()


class QRScanRequest(BaseModel):
    website_url: str
    company_name: str | None = None
    email: EmailStr


@router.post("/qr-scan")
async def qr_scan(payload: QRScanRequest):
    """Run the free PDPA scan and return a light AI summary (no PDF)."""
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
            "tier": "free",
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

        scan_result = await run_scan_async(website_url)
        scan_payload = scan_result.model_dump() if scan_result else {}
        ai_report = await ai_light(scan_payload)

        scan_data["scan_result"] = scan_payload
        scan_data["light_ai_report"] = ai_report
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

        try:
            report_row.status = "completed"
            db.commit()
        except Exception:
            db.rollback()

        return {
            "status": "completed",
            "report_id": str(report_row.id),
            "summary": ai_report,
            "message": "Free tier includes light AI summary only. PDF is not available.",
        }
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
