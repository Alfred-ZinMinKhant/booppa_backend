from app.core.route_classes import RetryAPIRoute
"""Verifiable offline artefacts — on-demand exportable PDF endpoints.

Closes the audit gap "declared but not verifiable offline": each dashboard-only
feature now has a downloadable, fileable PDF a vendor can attach to a tender. The
same PDFs are also emailed as attachments in the vendor digest.

  GET /vendor-artifacts/badge-certificate.pdf        (auth)        → Badge Certificate
  GET /vendor-artifacts/priority-placement.pdf       (auth)        → Priority Placement Report
  GET /vendor-artifacts/competitor-signals.pdf       (Vendor Pro)  → Competitor Activity Report
  GET /vendor-artifacts/bid-timing.pdf               (auth)        → Bid-Timing Report

Data assembly lives in `app/services/vendor_artifacts_builder.py` (shared with the
email task); these endpoints just stream the result.
"""
import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User
from app.services.vendor_artifacts_builder import (
    build_badge_certificate,
    build_priority_placement,
    build_bid_timing,
    build_competitor_signals,
)

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RetryAPIRoute)


def _pdf_response(pdf_bytes: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/badge-certificate.pdf")
def badge_certificate(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filename, pdf = build_badge_certificate(db, user)
    return _pdf_response(pdf, filename)


@router.get("/priority-placement.pdf")
def priority_placement(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filename, pdf = build_priority_placement(db, user)
    return _pdf_response(pdf, filename)


@router.get("/competitor-signals.pdf")
def competitor_signals_pdf(
    tenderNo: str = Query(..., description="GeBIZ tender number"),
    window_days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Vendor Pro only — same gate as the live endpoint.
    from app.billing.enforcement import VENDOR_PRO_PLAN_KEYS

    if (getattr(user, "plan", "") or "").lower().strip() not in VENDOR_PRO_PLAN_KEYS:
        raise HTTPException(status_code=403, detail="Vendor Pro subscription required.")
    filename, pdf = build_competitor_signals(db, user, tender_no=tenderNo, window_days=window_days)
    return _pdf_response(pdf, filename)


@router.get("/bid-timing.pdf")
def bid_timing_pdf(
    months_back: int = Query(12, ge=3, le=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filename, pdf = build_bid_timing(db, user, months_back=months_back)
    return _pdf_response(pdf, filename)
