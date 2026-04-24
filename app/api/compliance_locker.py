"""
Compliance Locker API — V11
============================
Endpoints that power the Singapore Compliance Locker page.

GET  /compliance/locker               → locker status for the authenticated user
GET  /compliance/regulations          → list of active Singapore regulations (seeded)
POST /compliance/dossier/generate     → generate a compliance dossier PDF for a set of regulations
GET  /compliance/evidence/{report_id} → lightweight evidence summary for one report
"""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import logging

from app.core.db import get_db, get_current_user
from app.core.models import Report

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Regulation seed data ───────────────────────────────────────────────────────
# Loaded once at import time — avoids a DB round-trip for the public endpoint.
# In production these should be seeded via Alembic data migration, but we also
# serve them directly here so the locker page works before the DB migration runs.

_SINGAPORE_REGULATIONS = [
    {
        "regulation_key": "PDPA",
        "display_name": "Personal Data Protection Act (PDPA)",
        "description": (
            "Singapore's primary data protection law. Requires consent for collection, "
            "purpose limitation, protection obligations, and DSAR handling."
        ),
        "required_frameworks": ["pdpa_scan", "pdpa_full", "pdpa_free_scan", "compliance_notarization"],
        "requires_notarization": False,
        "reference_url": "https://www.pdpc.gov.sg/Overview-of-PDPA/The-Legislation/Personal-Data-Protection-Act",
        "sort_order": 1,
    },
    {
        "regulation_key": "ACRA",
        "display_name": "ACRA Registration & Annual Filing",
        "description": (
            "Accounting and Corporate Regulatory Authority requirements: valid bizfile, "
            "annual return lodgment, and up-to-date director / shareholder records."
        ),
        "required_frameworks": ["acra_verification", "compliance_notarization"],
        "requires_notarization": False,
        "reference_url": "https://www.acra.gov.sg/",
        "sort_order": 2,
    },
    {
        "regulation_key": "GEBIZ",
        "display_name": "GeBIZ Government Procurement",
        "description": (
            "Government Electronic Business procurement portal. "
            "Vendors must be GeBIZ-registered and in good standing to bid for government contracts."
        ),
        "required_frameworks": ["gebiz_check", "compliance_notarization"],
        "requires_notarization": False,
        "reference_url": "https://www.gebiz.gov.sg/",
        "sort_order": 3,
    },
    {
        "regulation_key": "MAS",
        "display_name": "MAS Technology Risk Guidelines",
        "description": (
            "Monetary Authority of Singapore Technology Risk Management Guidelines "
            "for financial institutions and their critical IT service providers."
        ),
        "required_frameworks": ["mas_trm", "compliance_notarization"],
        "requires_notarization": True,
        "reference_url": "https://www.mas.gov.sg/regulation/guidelines/technology-risk-management-guidelines",
        "sort_order": 4,
    },
]


def _framework_matches(report_framework: str, required_frameworks: list[str], assessment_data: dict | None = None, regulation_key: str = "") -> bool:
    """
    Return True if the report's framework satisfies any of the required ones.
    Also counts if the report has a regulation_tag matching this regulation (tagged notarizations).
    """
    if report_framework in required_frameworks:
        return True
    # Tagged notarization: a compliance_notarization report tagged to this specific regulation
    if regulation_key and assessment_data and assessment_data.get("regulation_tag") == regulation_key:
        return True
    return False


def _score_evidence(reports: list, regulation: dict) -> dict:
    """
    Derive compliance status from the user's reports for one regulation.

    Returns:
        {
            "status": "MET" | "PARTIAL" | "MISSING",
            "evidence_count": int,
            "latest_evidence_date": str | None,
            "notarized": bool,
            "evidence_list": [ { report_id, framework, status, created_at, audit_hash } ]
        }
    """
    matching = [
        r for r in reports
        if _framework_matches(
            r.framework,
            regulation["required_frameworks"],
            r.assessment_data if isinstance(r.assessment_data, dict) else None,
            regulation["regulation_key"],
        )
        and r.status == "completed"
    ]

    notarized = any(
        bool(
            isinstance(r.assessment_data, dict)
            and r.assessment_data.get("blockchain_anchored")
        )
        for r in matching
    )

    status = "MISSING"
    if matching:
        if regulation["requires_notarization"] and not notarized:
            status = "PARTIAL"
        else:
            status = "MET"

    latest_date = None
    if matching:
        dates = [r.created_at for r in matching if r.created_at]
        if dates:
            latest_date = max(dates).isoformat()

    evidence_list = [
        {
            "report_id": str(r.id),
            "framework": r.framework,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "audit_hash": r.audit_hash,
            "notarized": bool(
                isinstance(r.assessment_data, dict)
                and r.assessment_data.get("blockchain_anchored")
            ),
        }
        for r in matching
    ]

    return {
        "status": status,
        "evidence_count": len(matching),
        "latest_evidence_date": latest_date,
        "notarized": notarized,
        "evidence_list": evidence_list,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/regulations")
async def list_regulations():
    """
    Public — returns the list of Singapore compliance regulations that Booppa tracks.
    Seeded from the in-memory constant so no DB round-trip is needed.
    """
    return {
        "regulations": _SINGAPORE_REGULATIONS,
        "total": len(_SINGAPORE_REGULATIONS),
    }


@router.get("/locker")
async def get_compliance_locker(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Authenticated — returns the full compliance locker for the logged-in user.

    For each Singapore regulation we check whether the user has completed reports
    that satisfy the regulation's required frameworks.

    Returns:
        {
            "user_id": str,
            "company_name": str,
            "overall_status": "MET" | "PARTIAL" | "MISSING",
            "met_count": int,
            "total_count": int,
            "compliance_score": int,   # 0–100
            "regulations": [
                {
                    "regulation_key": str,
                    "display_name": str,
                    "description": str,
                    "status": "MET" | "PARTIAL" | "MISSING",
                    "evidence_count": int,
                    "latest_evidence_date": str | None,
                    "notarized": bool,
                    "evidence_list": [...],
                    "requires_notarization": bool,
                    "reference_url": str,
                }
            ],
            "generated_at": str,
        }
    """
    user_id = str(current_user.id)

    # Fetch all completed reports for this user
    reports = (
        db.query(Report)
        .filter(
            Report.owner_id == user_id,
            Report.status == "completed",
        )
        .order_by(Report.created_at.desc())
        .all()
    )

    results = []
    met_count = 0

    for reg in _SINGAPORE_REGULATIONS:
        scored = _score_evidence(reports, reg)
        row = {
            **reg,
            **scored,
        }
        results.append(row)
        if scored["status"] == "MET":
            met_count += 1

    total = len(_SINGAPORE_REGULATIONS)
    compliance_score = round((met_count / total) * 100) if total else 0

    if met_count == total:
        overall_status = "MET"
    elif met_count > 0:
        overall_status = "PARTIAL"
    else:
        overall_status = "MISSING"

    return {
        "user_id": user_id,
        "company_name": getattr(current_user, "company", None) or getattr(current_user, "company_name", None) or "",
        "overall_status": overall_status,
        "met_count": met_count,
        "total_count": total,
        "compliance_score": compliance_score,
        "regulations": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/dossier/generate")
async def generate_dossier(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Authenticated — generate a compliance dossier summary for all MET regulations.

    Phase 1 implementation: returns a structured JSON dossier that the frontend
    can render as a printable/shareable compliance summary.
    Phase 2 will convert this to a signed PDF with blockchain anchoring.
    """
    user_id = str(current_user.id)

    reports = (
        db.query(Report)
        .filter(
            Report.owner_id == user_id,
            Report.status == "completed",
        )
        .order_by(Report.created_at.desc())
        .all()
    )

    met_regulations = []
    for reg in _SINGAPORE_REGULATIONS:
        scored = _score_evidence(reports, reg)
        if scored["status"] in ("MET", "PARTIAL"):
            met_regulations.append({
                "regulation_key": reg["regulation_key"],
                "display_name": reg["display_name"],
                "status": scored["status"],
                "evidence_count": scored["evidence_count"],
                "latest_evidence_date": scored["latest_evidence_date"],
                "notarized": scored["notarized"],
                "evidence_list": scored["evidence_list"],
            })

    return {
        "dossier_type": "singapore_compliance",
        "company_name": getattr(current_user, "company", None) or "",
        "user_id": user_id,
        "regulations_covered": met_regulations,
        "total_regulations": len(_SINGAPORE_REGULATIONS),
        "met_count": len(met_regulations),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",
        "note": (
            "This dossier is auto-generated from your evidence on file. "
            "PDF generation with blockchain anchoring is coming in Phase 2."
        ),
    }


@router.get("/evidence/{report_id}")
async def get_evidence_summary(
    report_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Authenticated — return a lightweight evidence summary for one report.
    Used by the locker page to show details on click.
    """
    user_id = str(current_user.id)

    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if str(report.owner_id) != user_id:
        raise HTTPException(status_code=403, detail="Not authorised.")

    assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}

    # Which regulations does this report satisfy?
    satisfies = [
        reg["regulation_key"]
        for reg in _SINGAPORE_REGULATIONS
        if _framework_matches(report.framework, reg["required_frameworks"])
    ]

    return {
        "report_id": str(report.id),
        "framework": report.framework,
        "company_name": report.company_name,
        "status": report.status,
        "audit_hash": report.audit_hash,
        "tx_hash": report.tx_hash,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
        "blockchain_anchored": bool(assessment.get("blockchain_anchored")),
        "pdf_url": report.s3_url if assessment.get("s3_uploaded") else None,
        "satisfies_regulations": satisfies,
    }
