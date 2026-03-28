"""
Tender Check API — V10
======================
GET /api/v1/tender-check
    ?tenderNo=ITQ123456
    &vendorId=<uuid>          (optional; omit for anonymous guest view)

Returns a TenderWinProbabilityResult JSON including the vendor's estimated
win probability, upgrade projections (RFP Express / RFP Complete), and gap
reasons derived from their current trust profile.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.core.db import get_db
from app.services.tender_service import compute_tender_win_probability

router = APIRouter()


@router.get("")
def tender_check(
    tenderNo: str = Query(..., description="GeBIZ tender number to evaluate"),
    vendorId: Optional[str] = Query(None, description="Vendor UUID (optional — omit for guest view)"),
    db: Session = Depends(get_db),
):
    """
    Estimate a vendor's probability of winning a GeBIZ tender.

    - **tenderNo**: Required. The GeBIZ tender reference (e.g. ``ITQ202500001``).
    - **vendorId**: Optional. Provide to receive personalised probability,
      projections, and gap analysis. Omit for a sector-baseline view.
    """
    result = compute_tender_win_probability(db, tenderNo, vendorId)

    if result.get("error") == "tender_not_found":
        raise HTTPException(status_code=404, detail=f"Tender '{tenderNo}' not found in shortlist")

    return result
