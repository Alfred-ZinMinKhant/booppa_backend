"""
Leaderboard & Rankings API Routes
==================================
Phase 3: Quarterly leaderboards, achievements, and prestige slots.
"""

from fastapi import APIRouter, Depends, Query
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.db import get_db
from app.services.feature_flags import require_feature
from app.services.leaderboard import (
    get_leaderboard, compute_quarterly_leaderboard,
    get_vendor_achievements, get_current_quarter,
)
from app.core.models import User
from app.core.models_v6 import VendorScore, VendorSector
from app.core.models_v8 import VendorStatusSnapshot

router = APIRouter()


@router.get("/leaderboard/all")
async def leaderboard_all(
    sector: Optional[str] = Query(None, description="Filter by sector/industry"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Get public leaderboard across all sectors for the Insights page."""
    q = (
        db.query(
            User.company,
            VendorScore.total_score,
            VendorStatusSnapshot.verification_depth,
            VendorStatusSnapshot.evidence_count,
            VendorSector.sector,
        )
        .join(VendorScore, VendorScore.vendor_id == User.id, isouter=True)
        .join(VendorStatusSnapshot, VendorStatusSnapshot.vendor_id == User.id, isouter=True)
        .join(VendorSector, VendorSector.vendor_id == User.id, isouter=True)
        .filter(User.is_active == True)
        .filter(User.role == "VENDOR")
    )
    if sector:
        q = q.filter(VendorSector.sector == sector)

    rows = q.order_by(VendorScore.total_score.desc().nullslast()).limit(limit).all()

    entries = [
        {
            "rank": idx + 1,
            "company": row.company or "Unknown",
            "sector": row.sector or "",
            "score": row.total_score or 0,
            "verification_depth": row.verification_depth or "UNVERIFIED",
            "evidence_count": row.evidence_count or 0,
        }
        for idx, row in enumerate(rows)
    ]

    return {"entries": entries}


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
