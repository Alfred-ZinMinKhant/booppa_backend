"""
Supply Chain / Managed Vendors API — V11
==========================================
Enterprise buyers track their vendor portfolio and compliance risk here.

GET    /supply-chain/portfolio            → full vendor portfolio for the buyer
POST   /supply-chain/vendors              → add a vendor to the portfolio
PATCH  /supply-chain/vendors/{id}         → update label / threshold / status
DELETE /supply-chain/vendors/{id}         → archive a vendor
GET    /supply-chain/vendors/{id}/refresh → re-pull compliance snapshot for one vendor
GET    /supply-chain/risk-summary         → aggregated risk counts across the portfolio
"""

from datetime import datetime, timezone
from typing import Optional
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User, Report
from app.core.models_v11 import ManagedVendor

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class AddVendorRequest(BaseModel):
    vendor_email: Optional[str] = None
    vendor_name: Optional[str] = None
    label: Optional[str] = None
    alert_threshold: str = "WATCH"  # CLEAN | WATCH | FLAGGED | CRITICAL


class UpdateVendorRequest(BaseModel):
    label: Optional[str] = None
    alert_threshold: Optional[str] = None
    status: Optional[str] = None  # ACTIVE | ARCHIVED


# ── Helpers ────────────────────────────────────────────────────────────────────

_RISK_FRAMEWORKS = [
    "pdpa_scan", "pdpa_full", "pdpa_free_scan",
    "compliance_notarization", "acra_verification",
]


def _compute_vendor_risk(vendor_user: Optional[User], db: Session) -> dict:
    """
    Derive a lightweight risk snapshot for a vendor.
    Returns cached fields to store on ManagedVendor.
    """
    if not vendor_user:
        return {
            "cached_risk_signal": "UNKNOWN",
            "cached_verification_depth": "none",
            "cached_procurement_readiness": "not_registered",
            "cached_total_score": None,
        }

    # Pull the vendor's completed reports
    reports = (
        db.query(Report)
        .filter(
            Report.owner_id == str(vendor_user.id),
            Report.status == "completed",
            Report.framework.in_(_RISK_FRAMEWORKS),
        )
        .order_by(Report.created_at.desc())
        .all()
    )

    notarized_count = sum(
        1 for r in reports
        if isinstance(r.assessment_data, dict) and r.assessment_data.get("blockchain_anchored")
    )

    report_count = len(reports)

    if report_count == 0:
        risk_signal = "FLAGGED"
        verification_depth = "none"
        procurement_readiness = "not_ready"
        score = 0
    elif report_count == 1 and notarized_count == 0:
        risk_signal = "WATCH"
        verification_depth = "basic"
        procurement_readiness = "partial"
        score = 40
    elif notarized_count > 0:
        risk_signal = "CLEAN"
        verification_depth = "blockchain_verified"
        procurement_readiness = "ready"
        score = 85 + min(notarized_count * 3, 15)
    else:
        risk_signal = "WATCH"
        verification_depth = "documented"
        procurement_readiness = "partial"
        score = 60

    return {
        "cached_risk_signal": risk_signal,
        "cached_verification_depth": verification_depth,
        "cached_procurement_readiness": procurement_readiness,
        "cached_total_score": min(score, 100),
    }


def _serialize_vendor(mv: ManagedVendor, vendor_user: Optional[User] = None) -> dict:
    return {
        "id": str(mv.id),
        "vendor_user_id": str(mv.vendor_user_id) if mv.vendor_user_id else None,
        "vendor_name": mv.vendor_name or (vendor_user.company if vendor_user and hasattr(vendor_user, "company") else None),
        "vendor_email": mv.vendor_email,
        "status": mv.status,
        "label": mv.label,
        "alert_threshold": mv.alert_threshold,
        "risk_signal": mv.cached_risk_signal,
        "verification_depth": mv.cached_verification_depth,
        "procurement_readiness": mv.cached_procurement_readiness,
        "compliance_score": mv.cached_total_score,
        "cache_refreshed_at": mv.cache_refreshed_at.isoformat() if mv.cache_refreshed_at else None,
        "added_at": mv.added_at.isoformat() if mv.added_at else None,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/portfolio")
async def get_portfolio(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Return the full managed vendor portfolio for the authenticated enterprise buyer.
    Only returns ACTIVE vendors by default (archived are excluded from the main view).
    """
    enterprise_id = str(current_user.id)

    vendors = (
        db.query(ManagedVendor)
        .filter(
            ManagedVendor.enterprise_user_id == enterprise_id,
            ManagedVendor.status == "ACTIVE",
        )
        .order_by(ManagedVendor.added_at.desc())
        .all()
    )

    result = []
    for mv in vendors:
        vendor_user = None
        if mv.vendor_user_id:
            vendor_user = db.query(User).filter(User.id == mv.vendor_user_id).first()
        result.append(_serialize_vendor(mv, vendor_user))

    # Aggregate risk counts
    risk_counts = {"CLEAN": 0, "WATCH": 0, "FLAGGED": 0, "CRITICAL": 0, "UNKNOWN": 0}
    for v in result:
        sig = v.get("risk_signal") or "UNKNOWN"
        risk_counts[sig] = risk_counts.get(sig, 0) + 1

    return {
        "enterprise_user_id": enterprise_id,
        "vendors": result,
        "total": len(result),
        "risk_summary": risk_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/vendors")
async def add_vendor(
    payload: AddVendorRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a vendor to the enterprise buyer's portfolio."""
    enterprise_id = str(current_user.id)

    if not payload.vendor_email and not payload.vendor_name:
        raise HTTPException(status_code=400, detail="Provide vendor_email or vendor_name.")

    # Look up vendor by email if provided
    vendor_user: Optional[User] = None
    if payload.vendor_email:
        vendor_user = db.query(User).filter(User.email == payload.vendor_email).first()

    # Prevent duplicate (same enterprise + same vendor user)
    if vendor_user:
        existing = db.query(ManagedVendor).filter(
            ManagedVendor.enterprise_user_id == enterprise_id,
            ManagedVendor.vendor_user_id == str(vendor_user.id),
        ).first()
        if existing:
            if existing.status == "ARCHIVED":
                existing.status = "ACTIVE"
                existing.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(existing)
                return {"created": False, "reactivated": True, "vendor": _serialize_vendor(existing, vendor_user)}
            raise HTTPException(status_code=409, detail="Vendor already in portfolio.")

    # Compute initial risk snapshot
    risk = _compute_vendor_risk(vendor_user, db)

    mv = ManagedVendor(
        id=uuid.uuid4(),
        enterprise_user_id=enterprise_id,
        vendor_user_id=str(vendor_user.id) if vendor_user else None,
        vendor_name=payload.vendor_name or (vendor_user.company if vendor_user and hasattr(vendor_user, "company") else None),
        vendor_email=payload.vendor_email,
        status="ACTIVE" if vendor_user else "PENDING_INVITE",
        label=payload.label,
        alert_threshold=payload.alert_threshold,
        cache_refreshed_at=datetime.now(timezone.utc),
        **risk,
    )
    db.add(mv)
    db.commit()
    db.refresh(mv)

    return {
        "created": True,
        "vendor": _serialize_vendor(mv, vendor_user),
    }


@router.patch("/vendors/{vendor_id}")
async def update_vendor(
    vendor_id: str,
    payload: UpdateVendorRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update label, alert threshold, or status for a managed vendor."""
    enterprise_id = str(current_user.id)

    mv = db.query(ManagedVendor).filter(
        ManagedVendor.id == vendor_id,
        ManagedVendor.enterprise_user_id == enterprise_id,
    ).first()
    if not mv:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    if payload.label is not None:
        mv.label = payload.label
    if payload.alert_threshold is not None:
        valid_thresholds = {"CLEAN", "WATCH", "FLAGGED", "CRITICAL"}
        if payload.alert_threshold not in valid_thresholds:
            raise HTTPException(status_code=400, detail=f"alert_threshold must be one of {valid_thresholds}")
        mv.alert_threshold = payload.alert_threshold
    if payload.status is not None:
        valid_statuses = {"ACTIVE", "ARCHIVED"}
        if payload.status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"status must be ACTIVE or ARCHIVED")
        mv.status = payload.status

    mv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mv)

    vendor_user = None
    if mv.vendor_user_id:
        vendor_user = db.query(User).filter(User.id == mv.vendor_user_id).first()

    return {"updated": True, "vendor": _serialize_vendor(mv, vendor_user)}


@router.delete("/vendors/{vendor_id}")
async def archive_vendor(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Archive (soft-delete) a vendor from the portfolio."""
    enterprise_id = str(current_user.id)

    mv = db.query(ManagedVendor).filter(
        ManagedVendor.id == vendor_id,
        ManagedVendor.enterprise_user_id == enterprise_id,
    ).first()
    if not mv:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    mv.status = "ARCHIVED"
    mv.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {"archived": True, "vendor_id": vendor_id}


@router.get("/vendors/{vendor_id}/refresh")
async def refresh_vendor_snapshot(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Re-pull and cache the compliance snapshot for one vendor."""
    enterprise_id = str(current_user.id)

    mv = db.query(ManagedVendor).filter(
        ManagedVendor.id == vendor_id,
        ManagedVendor.enterprise_user_id == enterprise_id,
    ).first()
    if not mv:
        raise HTTPException(status_code=404, detail="Vendor not found.")

    vendor_user = None
    if mv.vendor_user_id:
        vendor_user = db.query(User).filter(User.id == mv.vendor_user_id).first()

    risk = _compute_vendor_risk(vendor_user, db)
    mv.cached_risk_signal = risk["cached_risk_signal"]
    mv.cached_verification_depth = risk["cached_verification_depth"]
    mv.cached_procurement_readiness = risk["cached_procurement_readiness"]
    mv.cached_total_score = risk["cached_total_score"]
    mv.cache_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mv)

    return {"refreshed": True, "vendor": _serialize_vendor(mv, vendor_user)}


@router.get("/risk-summary")
async def get_risk_summary(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Aggregated risk signal counts for the enterprise buyer's portfolio."""
    enterprise_id = str(current_user.id)

    vendors = (
        db.query(ManagedVendor)
        .filter(
            ManagedVendor.enterprise_user_id == enterprise_id,
            ManagedVendor.status == "ACTIVE",
        )
        .all()
    )

    risk_counts = {"CLEAN": 0, "WATCH": 0, "FLAGGED": 0, "CRITICAL": 0, "UNKNOWN": 0}
    alerts = []

    for mv in vendors:
        sig = mv.cached_risk_signal or "UNKNOWN"
        risk_counts[sig] = risk_counts.get(sig, 0) + 1

        # Surface vendors that exceed the buyer's set alert threshold
        threshold_order = {"CLEAN": 0, "WATCH": 1, "FLAGGED": 2, "CRITICAL": 3, "UNKNOWN": 1}
        if threshold_order.get(sig, 0) >= threshold_order.get(mv.alert_threshold, 1):
            if sig in ("FLAGGED", "CRITICAL"):
                alerts.append({
                    "vendor_id": str(mv.id),
                    "vendor_name": mv.vendor_name or mv.vendor_email,
                    "risk_signal": sig,
                    "alert_threshold": mv.alert_threshold,
                })

    return {
        "total_active": len(vendors),
        "risk_counts": risk_counts,
        "alerts": alerts,
        "alert_count": len(alerts),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
