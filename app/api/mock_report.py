from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse
from io import BytesIO
from app.services.pdf_service import PDFService

router = APIRouter()


@router.get("/report", summary="Generate mock PDF report")
async def mock_report():
    """Return a generated PDF using mock data for local testing and previews."""
    svc = PDFService()
    mock = {
        "report_id": "MOCK-0001",
        "company_name": "Fictional Vendor Co.",
        "framework": "PDPA",
        "created_at": "2026-04-20T10:00:00+00:00",
        "status": "completed",
        "key_issues": [
            "Missing retention policy",
            "Unencrypted backups",
        ],
        "tx_hash": "0x8f3a12b4c9abcdef1234567890abcdef91c2",
        "audit_hash": "0xdeadbeefcafebabe",
        "verify_url": "https://polygonscan.com/tx/0x8f3a12b4c9abcdef1234567890abcdef91c2",
        "schema_version": "1.0",
        "executive_summary": "This is a mock executive summary demonstrating the PDF layout. No real data is included.",
        "detailed_findings": [
            {
                "type": "retention_policy",
                "severity": "MEDIUM",
                "description": "No formal data retention policy discovered.",
                "evidence": "Config file missing retention rules.",
            },
            {
                "type": "backup_encryption",
                "severity": "MEDIUM",
                "description": "Backups are stored unencrypted.",
                "evidence": "S3 bucket encryption not enabled.",
            },
        ],
        "recommendations": [
            {
                "violation_type": "retention_policy",
                "severity": "MEDIUM",
                "actions": [
                    "Create a documented retention policy",
                    "Implement automated deletion policies",
                ],
                "timeline": "30 days",
            },
        ],
    }

    pdf_bytes = svc.generate_pdf(mock)
    buf = BytesIO(pdf_bytes)
    headers = {
        "Content-Disposition": "attachment; filename=mock-report.pdf",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }
    return StreamingResponse(buf, media_type="application/pdf", headers=headers)
