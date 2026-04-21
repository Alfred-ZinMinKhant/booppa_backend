"""
RFP Requirements Routes — V8
=============================
Enterprise-only CRUD for procurement requirement specs + vendor evaluation.

POST   /api/rfp-requirements                    → create requirement
GET    /api/rfp-requirements                    → list (by auth'd enterprise user)
GET    /api/rfp-requirements/{id}               → get single
POST   /api/rfp-requirements/{id}/evaluate      → evaluate vendors (batch, up to 100)
GET    /api/rfp-requirements/{id}/flags         → get evaluation results
DELETE /api/rfp-requirements/{id}               → soft archive

DESIGN CONSTRAINTS:
  - Vendors are NEVER notified of any RFP requirement or flag
  - Evaluation result does NOT block any vendor from search results
  - All evaluation logic reads from VendorStatusSnapshot — no recalculation
"""

import uuid as _uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User, VendorScore, VerifyRecord
from app.core.models_v8 import (
    RfpRequirement,
    RfpRequirementFlag,
    VendorStatusSnapshot,
)

router = APIRouter()

DEPTH_ORDER = {
    "NONE":       -1,
    "UNVERIFIED":  0,
    "BASIC":       1,
    "STANDARD":    2,
    "DEEP":        3,
    "CERTIFIED":   4,
}


ENTERPRISE_PLANS = {"enterprise", "enterprise_pro", "standard_compliance", "pro_compliance"}


def _require_procurement(current_user):
    role = getattr(current_user, "role", "VENDOR")
    if role not in ("ADMIN", "PROCUREMENT"):
        raise HTTPException(status_code=403, detail="Procurement account required.")
    if role == "ADMIN":
        return
    plan = getattr(current_user, "plan", "free") or "free"
    if plan not in ENTERPRISE_PLANS:
        raise HTTPException(
            status_code=403,
            detail="Enterprise plan required. Upgrade to access procurement tools.",
        )


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RequirementCreate(BaseModel):
    label:                       str   = Field(..., min_length=1, max_length=100)
    description:                 Optional[str] = Field(None, max_length=500)
    minimum_verification_depth:  str   = Field(default="NONE")
    minimum_percentile:          float = Field(default=0.0, ge=0, le=100)
    require_active_monitoring:   bool  = Field(default=False)
    require_no_open_anomalies:   bool  = Field(default=False)
    minimum_days_until_expiry:   int   = Field(default=0, ge=0)


class EvaluateBody(BaseModel):
    vendor_ids: Optional[List[str]] = None
    slugs:      Optional[List[str]] = None


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("")
async def create_requirement(
    body:        RequirementCreate,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    _require_procurement(current_user)

    req = RfpRequirement(
        created_by_user_id           = current_user.id,
        label                        = body.label,
        description                  = body.description,
        minimum_verification_depth   = body.minimum_verification_depth,
        minimum_percentile           = body.minimum_percentile,
        require_active_monitoring    = body.require_active_monitoring,
        require_no_open_anomalies    = body.require_no_open_anomalies,
        minimum_days_until_expiry    = body.minimum_days_until_expiry,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return _requirement_to_dict(req)


@router.get("")
async def list_requirements(
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    _require_procurement(current_user)
    reqs = db.query(RfpRequirement).filter(
        RfpRequirement.created_by_user_id == current_user.id,
        RfpRequirement.archived == False,
    ).order_by(RfpRequirement.created_at.desc()).limit(50).all()
    return {"requirements": [_requirement_to_dict(r) for r in reqs]}


@router.get("/{requirement_id}")
async def get_requirement(
    requirement_id: str,
    db: Session     = Depends(get_db),
    current_user    = Depends(get_current_user),
):
    _require_procurement(current_user)
    req = _get_own_requirement(db, requirement_id, current_user.id)
    return _requirement_to_dict(req)


@router.post("/{requirement_id}/evaluate")
async def evaluate_vendors(
    requirement_id: str,
    body:           EvaluateBody,
    db: Session     = Depends(get_db),
    current_user    = Depends(get_current_user),
):
    """
    Evaluate up to 100 vendors against a requirement.
    Writes RfpRequirementFlag rows — does NOT block any vendor.
    Vendor is NEVER notified.
    """
    _require_procurement(current_user)
    req = _get_own_requirement(db, requirement_id, current_user.id)

    # Resolve vendor IDs
    vendor_ids: List[str] = list(body.vendor_ids or [])
    if body.slugs:
        for slug in body.slugs:
            user = db.query(User).filter(
                (User.company == slug) | (User.email.like(f"{slug}@%"))
            ).first()
            if user:
                vendor_ids.append(str(user.id))

    vendor_ids = list(dict.fromkeys(vendor_ids))[:100]  # deduplicate + cap

    if not vendor_ids:
        raise HTTPException(status_code=400, detail="No valid vendors to evaluate.")

    results = []
    for vid in vendor_ids:
        flag_details, overall = _evaluate_one(db, req, vid)

        # Upsert RfpRequirementFlag
        existing = db.query(RfpRequirementFlag).filter(
            RfpRequirementFlag.vendor_id == vid,
            RfpRequirementFlag.requirement_id == req.id,
        ).first()

        if existing:
            existing.overall_status = overall
            existing.flag_details   = flag_details
            existing.evaluated_at   = datetime.now(timezone.utc)
        else:
            db.add(RfpRequirementFlag(
                vendor_id      = vid,
                requirement_id = req.id,
                overall_status = overall,
                flag_details   = flag_details,
                evaluated_at   = datetime.now(timezone.utc),
            ))

        results.append({"vendorId": vid, "overallStatus": overall, "details": flag_details})

    db.commit()

    meets   = sum(1 for r in results if r["overallStatus"] == "MEETS")
    partial = sum(1 for r in results if r["overallStatus"] == "PARTIAL")
    missing = sum(1 for r in results if r["overallStatus"] == "MISSING")

    return {
        "requirementId": str(req.id),
        "evaluatedAt":   datetime.now(timezone.utc).isoformat(),
        "summary":       {"total": len(results), "meets": meets, "partial": partial, "missing": missing},
        "results":       results,
    }


@router.get("/{requirement_id}/flags")
async def get_flags(
    requirement_id: str,
    status:         Optional[str] = Query(None),
    limit:          int           = Query(50, ge=1, le=100),
    db: Session     = Depends(get_db),
    current_user    = Depends(get_current_user),
):
    _require_procurement(current_user)
    req = _get_own_requirement(db, requirement_id, current_user.id)

    flags_q = db.query(RfpRequirementFlag).filter(
        RfpRequirementFlag.requirement_id == req.id
    )
    if status:
        flags_q = flags_q.filter(RfpRequirementFlag.overall_status == status)
    flags = flags_q.limit(limit).all()

    return {
        "requirementId": str(req.id),
        "requirement":   {"label": req.label},
        "flags": [
            {
                "vendorId":      str(f.vendor_id),
                "overallStatus": f.overall_status,
                "details":       f.flag_details,
                "evaluatedAt":   f.evaluated_at.isoformat(),
            }
            for f in flags
        ],
        "count": len(flags),
    }


@router.delete("/{requirement_id}")
async def archive_requirement(
    requirement_id: str,
    db: Session     = Depends(get_db),
    current_user    = Depends(get_current_user),
):
    _require_procurement(current_user)
    req = _get_own_requirement(db, requirement_id, current_user.id)
    req.archived    = True
    req.archived_at = datetime.now(timezone.utc)
    db.commit()
    return {"archived": True, "id": str(req.id)}


# ── Private helpers ───────────────────────────────────────────────────────────

def _get_own_requirement(db: Session, requirement_id: str, user_id) -> RfpRequirement:
    req = db.query(RfpRequirement).filter(
        RfpRequirement.id == requirement_id,
        RfpRequirement.created_by_user_id == user_id,
        RfpRequirement.archived == False,
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    return req


def _evaluate_one(db: Session, req: RfpRequirement, vendor_id: str):
    """
    Evaluate a single vendor against a requirement.
    Returns (flag_details, overall_status).
    Reads from VendorStatusSnapshot — no recalculation.
    """
    status_snap = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id == vendor_id
    ).first()

    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == vendor_id
    ).first()

    flag_details = []
    fails        = 0
    partials     = 0

    # 1. Minimum verification depth
    if req.minimum_verification_depth and req.minimum_verification_depth != "NONE":
        actual_depth   = status_snap.verification_depth if status_snap else "UNVERIFIED"
        required_depth = req.minimum_verification_depth
        actual_rank    = DEPTH_ORDER.get(actual_depth, 0)
        required_rank  = DEPTH_ORDER.get(required_depth, 0)

        if actual_rank >= required_rank:
            dep_status = "MEETS"
        elif actual_rank >= required_rank - 1:
            dep_status = "PARTIAL"
            partials  += 1
        else:
            dep_status = "MISSING"
            fails     += 1

        flag_details.append({
            "requirementKey": "minimumVerificationDepth",
            "status":   dep_status,
            "actual":   actual_depth,
            "required": required_depth,
        })

    # 2. Minimum percentile
    if req.minimum_percentile and req.minimum_percentile > 0:
        actual_pct = status_snap.risk_adjusted_pct if status_snap else 0.0
        if actual_pct >= req.minimum_percentile:
            pct_status = "MEETS"
        elif actual_pct >= req.minimum_percentile * 0.75:
            pct_status = "PARTIAL"
            partials  += 1
        else:
            pct_status = "MISSING"
            fails     += 1
        flag_details.append({
            "requirementKey": "minimumPercentile",
            "status":   pct_status,
            "actual":   actual_pct,
            "required": req.minimum_percentile,
        })

    # 3. Active monitoring
    if req.require_active_monitoring:
        monitoring = status_snap.monitoring_activity if status_snap else "NONE"
        mon_status = "MEETS" if monitoring == "ACTIVE" else ("PARTIAL" if monitoring == "STALE" else "MISSING")
        if mon_status == "PARTIAL":
            partials += 1
        elif mon_status == "MISSING":
            fails    += 1
        flag_details.append({
            "requirementKey": "requireActiveMonitoring",
            "status":  mon_status,
            "actual":  monitoring,
            "required": "ACTIVE",
        })

    # 4. No open anomalies
    if req.require_no_open_anomalies:
        risk = status_snap.risk_signal if status_snap else "CLEAN"
        ano_status = "MEETS" if risk == "CLEAN" else ("PARTIAL" if risk == "WATCH" else "MISSING")
        if ano_status == "PARTIAL":
            partials += 1
        elif ano_status == "MISSING":
            fails    += 1
        flag_details.append({
            "requirementKey": "requireNoOpenAnomalies",
            "status":   ano_status,
            "actual":   risk,
            "required": "CLEAN",
        })

    # 5. Minimum days until expiry
    if req.minimum_days_until_expiry and req.minimum_days_until_expiry > 0:
        days_left = None
        if verify and verify.expires_at:
            exp_at = verify.expires_at if verify.expires_at.tzinfo else verify.expires_at.replace(tzinfo=timezone.utc)
            days_left = (exp_at - datetime.now(timezone.utc)).days
        exp_status = "MISSING"
        if days_left is not None and days_left >= req.minimum_days_until_expiry:
            exp_status = "MEETS"
        elif days_left is not None and days_left >= req.minimum_days_until_expiry * 0.5:
            exp_status = "PARTIAL"
            partials  += 1
        else:
            fails     += 1
        flag_details.append({
            "requirementKey": "minimumDaysUntilExpiry",
            "status":   exp_status,
            "actual":   days_left,
            "required": req.minimum_days_until_expiry,
        })

    # Overall status
    if fails > 0:
        overall = "MISSING"
    elif partials > 0:
        overall = "PARTIAL"
    else:
        overall = "MEETS"

    return flag_details, overall


def _requirement_to_dict(req: RfpRequirement) -> dict:
    return {
        "id":                         str(req.id),
        "label":                      req.label,
        "description":                req.description,
        "minimumVerificationDepth":   req.minimum_verification_depth,
        "minimumPercentile":          req.minimum_percentile,
        "requireActiveMonitoring":    req.require_active_monitoring,
        "requireNoOpenAnomalies":     req.require_no_open_anomalies,
        "minimumDaysUntilExpiry":     req.minimum_days_until_expiry,
        "archived":                   req.archived,
        "createdAt":                  req.created_at.isoformat() if req.created_at else None,
    }
