import asyncio, os
from app.services.booppa_ai_service import BooppaAIService
from app.services.pdf_service import PDFService


async def main():
    test_scan = {
        "company_name": "Test Company Singapore",
        "url": "http://testcompany.sg",
        "collects_nric": True,
        "has_legal_justification": False,
        "scan_date": "2024-01-15",
    }

    ai = BooppaAIService()
    report = await asyncio.to_thread(
        lambda: asyncio.run(ai.generate_compliance_report(test_scan))
    )

    pdf_payload = {
        "report_id": report.get("report_metadata", {}).get("report_id", "BOOPPA-TEST"),
        "framework": "PDPA",
        "company_name": report.get("company_info", {}).get("name", "Test"),
        "created_at": report.get("report_metadata", {}).get("generated_date"),
        "status": "completed",
        "key_issues": [d.get("type") for d in report.get("detailed_findings", [])],
        "ai_narrative": report.get("executive_summary", ""),
        "tx_hash": report.get("blockchain_evidence", {}).get("tx_hash"),
        "audit_hash": report.get("blockchain_evidence", {}).get("audit_hash"),
        "payment_confirmed": False,
        "contact_email": "test@example.com",
        "base_url": "http://localhost:8000",
    }

    pdf_service = PDFService()
    pdf_bytes = pdf_service.generate_pdf(pdf_payload)

    out_dir = "artifacts"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{pdf_payload['report_id']}.pdf")
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
