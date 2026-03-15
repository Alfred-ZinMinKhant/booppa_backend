"""
Leaderboard & Rankings API Routes
==================================
Phase 3: Quarterly leaderboards, achievements, and prestige slots.
"""

from fastapi import APIRouter, Depends, Query
from typing import Optional
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.feature_flags import require_feature
from app.services.leaderboard import (
    get_leaderboard, compute_quarterly_leaderboard,
    get_vendor_achievements, get_current_quarter,
)

router = APIRouter()


@router.get("/leaderboard/{sector}")
async def leaderboard(
    sector: str,
    quarter: Optional[str] = Query(None, description="Quarter e.g. 'Q1 2026'"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_RANKING")),
):
    """Get sector leaderboard."""
    return get_leaderboard(db, sector, quarter=quarter, limit=limit)


@router.post("/leaderboard/compute")
async def compute_leaderboard(
    quarter: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Compute quarterly leaderboard (admin)."""
    return compute_quarterly_leaderboard(db, quarter=quarter)


@router.get("/achievements/{vendor_id}")
async def achievements(
    vendor_id: str,
    db: Session = Depends(get_db),
):
    """Get vendor achievements."""
    return get_vendor_achievements(db, vendor_id)


@router.get("/current-quarter")
async def current_quarter():
    """Get current quarter string."""
    return {"quarter": get_current_quarter()}
