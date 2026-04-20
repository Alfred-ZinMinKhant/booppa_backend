"""
Comparison API Routes
=====================
Phase 2: Vendor comparison engine.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.feature_flags import require_feature
from app.services.vendor_comparison import compare_vendors, find_comparable_vendors

router = APIRouter()


@router.get("/")
async def compare_get(
    ids: str = Query(..., description="Comma-separated vendor IDs (2-4)"),
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_COMPARISON")),
):
    """Compare 2-4 vendors side-by-side (GET with comma-separated ids)."""
    vendor_ids = [v.strip() for v in ids.split(",") if v.strip()]
    return compare_vendors(db, vendor_ids)


from pydantic import BaseModel
from typing import List

class CompareRequest(BaseModel):
    vendor_ids: List[str]

@router.post("/")
async def compare_post(
    body: CompareRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_COMPARISON")),
):
    """Compare 2-4 vendors side-by-side (POST with JSON body)."""
    return compare_vendors(db, body.vendor_ids)


@router.get("/{vendor_id}/similar")
async def get_similar(
    vendor_id: str,
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_COMPARISON")),
):
    """Find comparable vendors in the same sector."""
    return find_comparable_vendors(db, vendor_id, limit=limit)
