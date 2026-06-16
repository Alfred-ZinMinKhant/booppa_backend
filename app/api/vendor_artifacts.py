"""Verifiable offline artefacts — on-demand exportable PDF endpoints.

Closes the audit gap "declared but not verifiable offline": each dashboard-only
feature now has a downloadable, fileable PDF a vendor can attach to a tender.

  GET /vendor-artifacts/badge-certificate.pdf        (auth)        → Badge Certificate
  GET /vendor-artifacts/priority-placement.pdf       (auth)        → Priority Placement Report
  GET /vendor-artifacts/competitor-signals.pdf       (Vendor Pro)  → Competitor Activity Report
  GET /vendor-artifacts/bid-timing.pdf               (auth)        → Bid-Timing Report

PDFs are generated live so the artefact is always current; the entity is always
the requesting CUSTOMER.
"""
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db, get_current_user
from app.core.models import User
from app.services.vendor_artifacts_generator import (
    generate_badge_certificate_pdf,
    generate_priority_placement_pdf,
    generate_competitor_signals_pdf,
    generate_bid_timing_pdf,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _pdf_response(pdf_bytes: bytes, filename: str) -> StreamingResponse:
    from io import BytesIO
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _company_of(user: User) -> str:
    return (getattr(user, "company", "") or "").strip() or "Your Company"


def _plan_label(user: User) -> str:
    plan = (getattr(user, "plan", "") or "").lower()
    if plan in ("vendor_pro", "vendor_pro_monthly", "vendor_pro_annual"):
        return "Vendor Pro"
    if plan in ("vendor_active", "vendor_active_monthly", "vendor_active_annual"):
        return "Vendor Active"
    return "Vendor"


@router.get("/badge-certificate.pdf")
def badge_certificate(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.core.models_v8 import VendorStatusSnapshot

    snap = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == user.id)
        .first()
    )
    verify_base = (getattr(settings, "VERIFY_BASE_URL", "https://www.booppa.io") or "https://www.booppa.io").rstrip("/")
    pdf = generate_badge_certificate_pdf({
        "company_name": _company_of(user),
        "verification_depth": getattr(snap, "verification_depth", None) or "BASIC",
        "procurement_readiness": getattr(snap, "procurement_readiness", None) or "CONDITIONAL",
        "confidence_score": getattr(snap, "confidence_score", None),
        "vendor_id": str(user.id),
        "verify_url": f"{verify_base}/verify/{user.id}",
    })
    return _pdf_response(pdf, "BOOPPA-Badge-Certificate.pdf")


@router.get("/priority-placement.pdf")
def priority_placement(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.core.models import VerifyRecord, ProofView
    from app.core.models_v8 import VendorStatusSnapshot

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    profile_views = 0
    if verify:
        profile_views = (
            db.query(ProofView)
            .filter(ProofView.verify_id == verify.id, ProofView.created_at >= thirty_days_ago)
            .count()
        )
    snap = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == user.id)
        .first()
    )
    plan_label = _plan_label(user)
    pdf = generate_priority_placement_pdf({
        "company_name": _company_of(user),
        "plan_label": plan_label,
        "profile_views_30d": profile_views,
        "verification_depth": getattr(snap, "verification_depth", None) or "BASIC",
        "placement_active": plan_label in ("Vendor Active", "Vendor Pro"),
    })
    return _pdf_response(pdf, "BOOPPA-Priority-Placement-Report.pdf")


@router.get("/competitor-signals.pdf")
def competitor_signals_pdf(
    tenderNo: str = Query(..., description="GeBIZ tender number"),
    window_days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Vendor Pro only — reuse the same gate as the live endpoint.
    from app.billing.enforcement import VENDOR_PRO_PLAN_KEYS

    if (getattr(user, "plan", "") or "").lower().strip() not in VENDOR_PRO_PLAN_KEYS:
        raise HTTPException(status_code=403, detail="Vendor Pro subscription required.")

    # Reuse the live competitor-signals computation so the PDF matches the dashboard.
    from app.api.vendor_pro import competitor_signals as _live_signals

    signals = _live_signals(tenderNo=tenderNo, window_days=window_days, db=db, user=user)
    pdf = generate_competitor_signals_pdf({
        "company_name": _company_of(user),
        "tender_no": signals.get("tender_no"),
        "window_days": signals.get("window_days"),
        "lookups": signals.get("lookups"),
        "sector": signals.get("sector"),
        "sector_active_verified": signals.get("sector_active_verified"),
    })
    return _pdf_response(pdf, "BOOPPA-Competitor-Activity-Report.pdf")


@router.get("/bid-timing.pdf")
def bid_timing_pdf(
    months_back: int = Query(12, ge=3, le=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.core.models_gebiz import GebizAwardHistory

    since = (datetime.now(timezone.utc) - timedelta(days=30 * months_back)).date()
    rows = (
        db.query(GebizAwardHistory)
        .filter(GebizAwardHistory.awarded_date != None, GebizAwardHistory.awarded_date >= since)  # noqa: E711
        .all()
    )

    # Aggregate awards-by-month (chronological).
    buckets: "OrderedDict[str, dict]" = OrderedDict()
    for r in sorted(rows, key=lambda x: x.awarded_date):
        key = r.awarded_date.strftime("%b %Y")
        b = buckets.setdefault(key, {"month": key, "awards": 0, "value": 0.0})
        b["awards"] += 1
        try:
            b["value"] += float(r.award_amt or 0)
        except (TypeError, ValueError):
            pass

    months = list(buckets.values())
    busiest = max(months, key=lambda m: m["awards"])["month"] if months else "—"
    period_label = (
        f"GeBIZ awards, {months[0]['month']} – {months[-1]['month']}"
        if months else "GeBIZ award history"
    )
    pdf = generate_bid_timing_pdf({
        "company_name": _company_of(user),
        "period_label": period_label,
        "total_awards": len(rows),
        "busiest_month": busiest,
        "months": months,
    })
    return _pdf_response(pdf, "BOOPPA-Bid-Timing-Report.pdf")
