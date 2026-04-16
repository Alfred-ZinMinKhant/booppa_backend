"""
Funnel & Analytics API Routes
=============================
Funnel tracking, revenue analytics, and admin dashboard data.
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.funnel_analytics import (
    record_funnel_event, get_funnel_summary,
    get_revenue_summary, compute_monthly_snapshot,
)

router = APIRouter()


class FunnelEventRequest(BaseModel):
    stage: str
    session_id: Optional[str] = None
    source: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    metadata: Optional[dict] = None


@router.post("/track")
@router.post("/event")
async def track_funnel_event(
    body: FunnelEventRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Track a funnel event."""
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent", "")[:500]

    event = record_funnel_event(
        db,
        stage=body.stage,
        session_id=body.session_id,
        source=body.source,
        utm_source=body.utm_source,
        utm_medium=body.utm_medium,
        utm_campaign=body.utm_campaign,
        ip_address=ip,
        user_agent=ua,
        metadata=body.metadata,
    )
    return {"id": str(event.id), "stage": event.stage}


@router.get("/summary")
async def funnel_summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Get funnel conversion summary."""
    return get_funnel_summary(db, days=days)


@router.get("/revenue")
async def revenue_summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Get revenue summary."""
    return get_revenue_summary(db, days=days)


@router.post("/snapshot/{month}")
async def compute_snapshot(
    month: str,
    db: Session = Depends(get_db),
):
    """Compute monthly subscription snapshot. Format: '2026-03'."""
    snapshot = compute_monthly_snapshot(db, month)
    return {
        "month": snapshot.month,
        "total_mrr_cents": snapshot.total_mrr_cents,
        "new_mrr_cents": snapshot.new_mrr_cents,
        "expansion_cents": snapshot.expansion_cents,
        "contraction_cents": snapshot.contraction_cents,
        "churn_cents": snapshot.churn_cents,
    }
