"""
Leaderboard Service
===================
Quarterly leaderboard computation, achievements, milestones, and prestige slots.
"""

import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.core.models import User
from app.core.models_v6 import VendorScore, VendorSector
from app.core.models_v8 import VendorStatusSnapshot
from app.core.models_v10 import (
    QuarterlyLeaderboard, Achievement, ScoreMilestone, PrestigeSlot,
)

logger = logging.getLogger(__name__)


def get_current_quarter() -> str:
    """Get current quarter string e.g. 'Q1 2026'."""
    now = datetime.utcnow()
    q = (now.month - 1) // 3 + 1
    return f"Q{q} {now.year}"


def compute_quarterly_leaderboard(db: Session, quarter: Optional[str] = None) -> dict:
    """Compute quarterly leaderboard for all sectors."""
    quarter = quarter or get_current_quarter()

    # Get all sectors
    sectors = (
        db.query(VendorSector.sector)
        .distinct()
        .filter(VendorSector.sector.isnot(None))
        .all()
    )

    total_created = 0

    for (sector,) in sectors:
        # Get all vendors in this sector ranked by score
        results = (
            db.query(VendorScore, User)
            .join(User, VendorScore.vendor_id == User.id)
            .join(VendorSector, VendorScore.vendor_id == VendorSector.vendor_id)
            .filter(VendorSector.sector == sector, User.role == "vendor")
            .order_by(desc(VendorScore.total_score))
            .all()
        )

        if not results:
            continue

        sector_count = len(results)

        for rank, (score, user) in enumerate(results, 1):
            percentile = round((1 - rank / sector_count) * 100, 1)

            # Determine tier
            if percentile >= 90:
                tier = "ELITE"
            elif percentile >= 70:
                tier = "STRATEGIC"
            else:
                tier = "STANDARD"

            # Determine trophy
            if rank == 1:
                trophy = "GOLD"
            elif rank == 2:
                trophy = "SILVER"
            elif rank == 3:
                trophy = "BRONZE"
            else:
                trophy = "NONE"

            is_top = rank <= 5

            # Upsert leaderboard entry
            existing = db.query(QuarterlyLeaderboard).filter(
                QuarterlyLeaderboard.vendor_id == user.id,
                QuarterlyLeaderboard.quarter == quarter,
                QuarterlyLeaderboard.sector == sector,
            ).first()

            if existing:
                existing.rank = rank
                existing.final_score = score.total_score
                existing.percentile = percentile
                existing.tier = tier
                existing.is_top_vendor = is_top
                existing.trophy = trophy
            else:
                entry = QuarterlyLeaderboard(
                    vendor_id=user.id,
                    quarter=quarter,
                    sector=sector,
                    rank=rank,
                    final_score=score.total_score,
                    percentile=percentile,
                    tier=tier,
                    is_top_vendor=is_top,
                    trophy=trophy,
                )
                db.add(entry)
                total_created += 1

            # Award achievements for top performers
            if percentile >= 90:
                _award_achievement(db, user.id, "TOP_10_PCT", quarter, sector,
                                   f"Top 10% in {sector} — {quarter}")
            if percentile >= 95:
                _award_achievement(db, user.id, "TOP_5_PCT", quarter, sector,
                                   f"Top 5% in {sector} — {quarter}")
            if percentile >= 99:
                _award_achievement(db, user.id, "TOP_1_PCT", quarter, sector,
                                   f"Top 1% in {sector} — {quarter}")

            # Prestige slots for top 5 ELITE
            if tier == "ELITE" and rank <= 5:
                _assign_prestige_slot(db, user.id, sector, tier, rank, quarter)

    db.commit()
    return {"quarter": quarter, "sectors_processed": len(sectors), "entries_created": total_created}


def _award_achievement(db: Session, vendor_id, achievement_type: str,
                       quarter: str, sector: str, label: str):
    """Award an achievement if not already awarded."""
    existing = db.query(Achievement).filter(
        Achievement.vendor_id == vendor_id,
        Achievement.achievement_type == achievement_type,
        Achievement.quarter == quarter,
        Achievement.sector == sector,
    ).first()

    if not existing:
        db.add(Achievement(
            vendor_id=vendor_id,
            achievement_type=achievement_type,
            label=label,
            quarter=quarter,
            sector=sector,
        ))


def _assign_prestige_slot(db: Session, vendor_id, sector: str, tier: str,
                          slot_number: int, quarter: str):
    """Assign a prestige slot."""
    existing = db.query(PrestigeSlot).filter(
        PrestigeSlot.sector == sector,
        PrestigeSlot.tier == tier,
        PrestigeSlot.slot_number == slot_number,
        PrestigeSlot.quarter == quarter,
    ).first()

    if existing:
        existing.vendor_id = vendor_id
        existing.is_active = True
    else:
        db.add(PrestigeSlot(
            vendor_id=vendor_id,
            sector=sector,
            tier=tier,
            slot_number=slot_number,
            quarter=quarter,
        ))


def get_leaderboard(db: Session, sector: str, quarter: Optional[str] = None,
                    limit: int = 20) -> dict:
    """Get leaderboard for a sector."""
    quarter = quarter or get_current_quarter()

    entries = (
        db.query(QuarterlyLeaderboard, User)
        .join(User, QuarterlyLeaderboard.vendor_id == User.id)
        .filter(
            QuarterlyLeaderboard.sector == sector,
            QuarterlyLeaderboard.quarter == quarter,
        )
        .order_by(QuarterlyLeaderboard.rank)
        .limit(limit)
        .all()
    )

    return {
        "sector": sector,
        "quarter": quarter,
        "entries": [
            {
                "rank": e.rank,
                "company": u.company or u.full_name,
                "vendor_id": str(e.vendor_id),
                "final_score": e.final_score,
                "percentile": e.percentile,
                "tier": e.tier,
                "trophy": e.trophy,
                "is_top_vendor": e.is_top_vendor,
            }
            for e, u in entries
        ],
    }


def get_vendor_achievements(db: Session, vendor_id: str) -> list[dict]:
    """Get all achievements for a vendor."""
    achievements = (
        db.query(Achievement)
        .filter(Achievement.vendor_id == vendor_id)
        .order_by(Achievement.awarded_at.desc())
        .all()
    )

    return [
        {
            "id": str(a.id),
            "type": a.achievement_type,
            "label": a.label,
            "description": a.description,
            "quarter": a.quarter,
            "sector": a.sector,
            "awarded_at": a.awarded_at.isoformat() if a.awarded_at else None,
        }
        for a in achievements
    ]
