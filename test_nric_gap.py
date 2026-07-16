import asyncio
from app.core.db import SessionLocal
from app.core.models import Report
from app.services.evidence_pack.document_generator import generate_document

async def main():
    db = SessionLocal()
    # Find ensigninfosecurity report with pdpa_quick_scan
    r = db.query(Report).filter(
        Report.company_name.ilike('%ensign%'),
        Report.framework == 'pdpa_quick_scan'
    ).order_by(Report.created_at.desc()).first()

    if not r:
        print("No report found for ensign")
        return

    # Check NRIC score in assessment_data
    nric_score = None
    if isinstance(r.assessment_data, dict):
        if "nric_evidence" in r.assessment_data:
            nric_score = r.assessment_data["nric_evidence"].get("score")
            print(f"NRIC Score found: {nric_score}")

    intake = {
        "org_name": r.company_name,
        "website": "ensigninfosecurity.com",
        "sector": "Technology",
        "dpo_name": "Test DPO",
        "scan_evidence": {"pdpa_report": r.assessment_data} if r.assessment_data else {}
    }
    
    # We will just print the exact prompt being sent to the LLM
    from app.services.evidence_pack.document_generator import _evidence_context, _prompt_vendor_register
    
    context = _evidence_context(intake.get("scan_evidence"))
    print("--- EVIDENCE CONTEXT ---")
    print(context)

if __name__ == "__main__":
    asyncio.run(main())
