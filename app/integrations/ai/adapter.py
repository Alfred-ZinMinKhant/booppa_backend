from typing import Any

from app.services.booppa_ai_service import BooppaAIService


async def ai_light(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Medium compliance risk detected.",
        "recommendation": "Review data transfer mechanisms.",
        "detected_laws": scan.get("detected_laws", []),
        "risk_score": scan.get("overall_risk_score"),
    }


def _build_ai_scan_payload(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_name": scan.get("company_name") or "Not specified",
        "url": scan.get("url"),
        "scan_date": scan.get("scan_date"),
        "collects_nric": scan.get("nric_found", False),
        "has_legal_justification": scan.get("has_legal_justification", False),
        "uses_https": scan.get("uses_https", True),
        "detected_laws": scan.get("detected_laws", []),
        "overall_risk_score": scan.get("overall_risk_score"),
    }


async def ai_full(scan: dict[str, Any]) -> dict[str, Any]:
    ai_service = BooppaAIService()
    payload = _build_ai_scan_payload(scan)
    report = await ai_service.generate_compliance_report(payload)
    report["detected_laws"] = scan.get("detected_laws", [])
    return report
