"""
Vendor Dashboard — real data endpoint
======================================
GET /api/v1/dashboard

Returns:
  stats:          trustScore, enterpriseViews (7d), activeProcurements, govAgencies
  chartData:      7-day daily view + trigger counts
  recentActivity: last 10 proof views with domain / intent label
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

# Day-of-week labels (Mon = 0)
_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Intent classification by domain keywords
_GOV_KEYWORDS = (".gov.sg", ".gov", "gebiz", "iras", "mof.", "mti.", "defence.", "mindef")


def _classify_view(domain: str | None, visit_count: int) -> tuple[str, str, str]:
    """Return (label, text-colour, bg-colour) for a proof view."""
    d = (domain or "").lower()
    if any(k in d for k in _GOV_KEYWORDS):
        return "Gov Agency View", "text-purple-400", "bg-purple-400/10"
    if visit_count >= 3:
        return "Repeated Visit", "text-amber-400", "bg-amber-400/10"
    return "Enterprise View", "text-emerald-400", "bg-emerald-400/10"


def _relative_time(dt: datetime) -> str:
    delta = datetime.utcnow() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60} mins ago"
    if secs < 86400:
        return f"{secs // 3600} hrs ago"
    return f"{delta.days}d ago"


@router.get("")
async def dashboard(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    vendor_id = current_user.id

    # ── 1. Trust score ────────────────────────────────────────────────────────
    from app.core.models_v6 import VendorScore, VerifyRecord, ProofView, EnterpriseProfile, GovernanceRecord
    from app.core.models_v6 import GeBizActivity

    score_row = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
    trust_score = score_row.total_score if score_row else 0

    # ── 2. Verify record → proof views ───────────────────────────────────────
    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
    cutoff_7d = datetime.utcnow() - timedelta(days=7)

    enterprise_views = 0
    gov_agency_domains: set[str] = set()
    chart_data: List[Dict[str, Any]] = []
    recent_activity: List[Dict[str, Any]] = []

    if verify:
        # Enterprise views in last 7 days (UNIQUE DOMAINS)
        enterprise_views = (
            db.query(func.count(func.distinct(ProofView.domain)))
            .filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= cutoff_7d,
                ProofView.domain.isnot(None)
            )
            .scalar()
            or 0
        )

        # 7-day chart: daily view counts + triggers
        daily_rows = (
            db.query(
                func.date(ProofView.created_at).label("day"),
                func.count(ProofView.id).label("views"),
                func.count(func.distinct(ProofView.domain)).label("unique_views")
            )
            .filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= cutoff_7d,
            )
            .group_by(func.date(ProofView.created_at))
            .all()
        )
        day_map = {str(r.day): r.views for r in daily_rows}
        
        # Pull real triggers from GovernanceRecord
        trigger_rows = (
            db.query(
                func.date(GovernanceRecord.timestamp).label("day"),
                func.count(GovernanceRecord.id).label("count")
            )
            .filter(
                GovernanceRecord.event_type == 'PROCUREMENT_WINDOW',
                GovernanceRecord.timestamp >= cutoff_7d
            )
            .group_by(func.date(GovernanceRecord.timestamp))
            .all()
        )
        trigger_map = {str(r.day): r.count for r in trigger_rows}

        for i in range(7):
            day_dt = datetime.utcnow() - timedelta(days=6 - i)
            day_key = day_dt.strftime("%Y-%m-%d")
            chart_data.append({
                "name":     _DAY_LABELS[day_dt.weekday()],
                "views":    day_map.get(day_key, 0),
                "triggers": trigger_map.get(day_key, 0)
            })

        # Gov agencies that viewed this vendor's proof
        gov_views = (
            db.query(ProofView.domain)
            .filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= cutoff_7d,
            )
            .distinct()
            .all()
        )
        for (domain,) in gov_views:
            d = (domain or "").lower()
            if any(k in d for k in _GOV_KEYWORDS):
                gov_agency_domains.add(domain)

        # Recent activity — last 10 views
        recent_rows = (
            db.query(ProofView)
            .filter(ProofView.verify_id == verify.id)
            .order_by(ProofView.created_at.desc())
            .limit(10)
            .all()
        )
        domain_visit_counts: Dict[str, int] = {}
        for row in recent_rows:
            d = row.domain or "unknown"
            domain_visit_counts[d] = domain_visit_counts.get(d, 0) + 1

        for row in recent_rows:
            d = row.domain or "unknown"
            label, color, bg = _classify_view(d, domain_visit_counts[d])
            recent_activity.append({
                "domain": d,
                "type":   label,
                "time":   _relative_time(row.created_at),
                "color":  color,
                "bg":     bg,
            })

    # ── 3. Active procurements (GeBiz signals in last 30d) ───────────────────
    cutoff_30d = datetime.utcnow() - timedelta(days=30)
    active_procurements = (
        db.query(func.count(GeBizActivity.id))
        .filter(
            GeBizActivity.vendor_id == vendor_id,
            GeBizActivity.created_at >= cutoff_30d,
        )
        .scalar()
        or 0
    )

    return {
        "stats": {
            "trustScore":          trust_score,
            "enterpriseViews":     enterprise_views,
            "activeProcurements":  active_procurements,
            "govAgencies":         len(gov_agency_domains),
        },
        "chartData":      chart_data,
        "recentActivity": recent_activity,
    }
