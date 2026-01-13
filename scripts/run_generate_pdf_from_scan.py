import asyncio
from app.services.booppa_ai_service import BooppaAIService
from app.services.pdf_service import PDFService
from datetime import datetime
import os

os.makedirs("artifacts", exist_ok=True)

scan_data = {
    "company_name": "Booppa",
    "url": "https://www.booppa.io/",
    "scan_date": "2025-12-23",
    "summary": "Low risk - Your website protects privacy",
    "regulations": ["GDPR", "ePR"],
    "trackers": [
        {"name": "__cf_bm", "provider": "calendly.com", "category": "Necessary"},
        {"name": "_cfuvid", "provider": "calendly.com", "category": "Necessary"},
        {"name": "__cf_bm", "provider": "calendly.com", "category": "Necessary"},
        {"name": "_cfuvid", "provider": "calendly.com", "category": "Necessary"},
        {
            "name": "_calendly_session",
            "provider": "calendly.com",
            "category": "Necessary",
        },
    ],
    "uses_https": True,
    "collects_nric": False,
    "consent_mechanism": {"has_cookie_banner": True, "has_active_consent": False},
    "dpo_compliance": {"has_dpo": True},
    "dnc_mention": {"mentions_dnc": True},
}


async def run():
    ai = BooppaAIService()
    report = await ai.generate_compliance_report(scan_data)

    pdf_data = {
        "report_id": report["report_metadata"]["report_id"],
        "framework": "PDPA/GDPR",
        "company_name": report["company_info"]["name"],
        "created_at": datetime.utcnow().isoformat(),
        "status": "completed",
        "tx_hash": None,
        "audit_hash": "",
        # include the full structured report so PDFService can render all sections
        "structured_report": report,
        "ai_narrative": report.get("executive_summary", ""),
        "payment_confirmed": False,
        "contact_email": None,
        "base_url": scan_data.get("url", "https://www.booppa.io"),
        "key_issues": [
            f"{f['severity']}: {f['type'].replace('_',' ').title()}"
            for f in report.get("detailed_findings", [])[:5]
        ],
    }

    pdf_service = PDFService()
    pdf_bytes = pdf_service.generate_pdf(pdf_data)

    out_path = f"artifacts/{pdf_data['report_id']}.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)

    print("PDF written:", out_path)


if __name__ == "__main__":
    asyncio.run(run())
