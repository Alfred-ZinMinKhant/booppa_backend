from datetime import datetime
import logging
import uuid

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr

from app.services.booppa_ai_service import BooppaAIService
from app.services.pdf_service import PDFService

logger = logging.getLogger(__name__)

router = APIRouter()


class QRScanRequest(BaseModel):
    website_url: str
    company_name: str | None = None
    email: EmailStr


@router.post("/qr-scan")
async def qr_scan(payload: QRScanRequest):
    """Run the free PDPA scan and return a PDF report."""
    try:
        website_url = payload.website_url.strip()
        uses_https = website_url.lower().startswith("https://")

        scan_data = {
            "company_name": payload.company_name or "Free PDPA Scan",
            "url": website_url,
            "scan_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "uses_https": uses_https,
            "assessment_source": "free_scan",
            "contact_email": payload.email,
        }

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

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="Booppa-PDPA-Scan.pdf"'
            },
        )
    except Exception as e:
        logger.error(f"Free scan failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate scan report")
