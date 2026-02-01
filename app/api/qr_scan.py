from datetime import datetime, timedelta
import logging
import uuid

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr

from app.core.db import SessionLocal
from app.core.models import Report
from app.services.booppa_ai_service import BooppaAIService
from app.services.pdf_service import PDFService
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

        booppa = BooppaAIService()
        report = await booppa.generate_compliance_report(scan_data)

        report_id = f"FREE-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        pdf_payload = {
            "report_id": report_id,
            "framework": "PDPA Quick Scan (Free)",
            "company_name": scan_data.get("company_name"),
            "created_at": datetime.utcnow().isoformat(),
            "status": "completed",
            "tx_hash": None,
            "audit_hash": None,
            "ai_narrative": report.get("executive_summary", ""),
            "structured_report": report,
            "payment_confirmed": False,
            "contact_email": payload.email,
            "key_issues": [
                f"{f.get('severity', 'MEDIUM')}: {f.get('type', '').replace('_', ' ').title()}"
                for f in report.get("detailed_findings", [])[:5]
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
