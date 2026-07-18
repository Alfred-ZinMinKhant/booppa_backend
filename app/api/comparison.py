from app.core.route_classes import RetryAPIRoute
"""
Comparison API Routes
=====================
Phase 2: Vendor comparison engine.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db import get_db, get_current_user
from app.api.procurement import _require_procurement
from app.services.vendor_comparison import compare_vendors, find_comparable_vendors

router = APIRouter(route_class=RetryAPIRoute)


@router.get("/")
async def compare_get(
    ids: str = Query(..., description="Comma-separated vendor IDs (2-4)"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Compare 2-4 vendors side-by-side (GET with comma-separated ids)."""
    _require_procurement(current_user)
    vendor_ids = [v.strip() for v in ids.split(",") if v.strip()]
    return compare_vendors(db, vendor_ids, buyer_user=current_user)


from pydantic import BaseModel
from typing import List


class CompareRequest(BaseModel):
    vendor_ids: List[str]


@router.post("/")
async def compare_post(
    body: CompareRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Compare 2-4 vendors side-by-side (POST with JSON body)."""
    _require_procurement(current_user)
    return compare_vendors(db, body.vendor_ids, buyer_user=current_user)


@router.get("/{vendor_id}/similar")
async def get_similar(
    vendor_id: str,
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Find comparable vendors in the same sector."""
    _require_procurement(current_user)
    return find_comparable_vendors(db, vendor_id, limit=limit)
