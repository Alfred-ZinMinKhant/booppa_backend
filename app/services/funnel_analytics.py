"""
Funnel & Analytics Service
==========================
Conversion funnel tracking, revenue analytics, and cohort analysis.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from app.core.models_v10 import FunnelEvent, RevenueEvent, SubscriptionSnapshot

logger = logging.getLogger(__name__)


def record_funnel_event(
    db: Session,
    stage: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> FunnelEvent:
    """Record a funnel event."""
    # Determine previous stage for this user/session
    previous = None
    if user_id:
        prev = (
            db.query(FunnelEvent)
            .filter(FunnelEvent.user_id == user_id)
            .order_by(FunnelEvent.created_at.desc())
            .first()
        )
        if prev:
            previous = prev.stage
    elif session_id:
        prev = (
            db.query(FunnelEvent)
            .filter(FunnelEvent.session_id == session_id)
            .order_by(FunnelEvent.created_at.desc())
            .first()
        )
        if prev:
            previous = prev.stage

    event = FunnelEvent(
        user_id=user_id,
        session_id=session_id,
        stage=stage,
        previous_stage=previous,
        source=source,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata_json=metadata,
    )
    db.add(event)
    db.commit()
    return event


def get_funnel_summary(db: Session, days: int = 30) -> dict:
    """Get funnel conversion summary for the last N days."""
    since = datetime.utcnow() - timedelta(days=days)

    stages = ["VISIT", "SIGNUP", "TRIAL", "SCAN", "CHECKOUT", "PAYMENT", "VERIFICATION", "ACTIVE"]
    counts = {}
    for stage in stages:
        counts[stage] = (
            db.query(func.count(FunnelEvent.id))
            .filter(FunnelEvent.stage == stage, FunnelEvent.created_at >= since)
            .scalar()
        )

    # Calculate conversion rates
    conversions = {}
    for i in range(1, len(stages)):
        prev_count = counts[stages[i - 1]]
        curr_count = counts[stages[i]]
        conversions[f"{stages[i-1]}_to_{stages[i]}"] = (
            round(curr_count / prev_count * 100, 1) if prev_count > 0 else 0
        )

    return {
        "period_days": days,
        "stage_counts": counts,
        "conversion_rates": conversions,
        "total_visits": counts.get("VISIT", 0),
        "total_conversions": counts.get("PAYMENT", 0),
        "overall_conversion_rate": round(
            counts.get("PAYMENT", 0) / counts.get("VISIT", 1) * 100, 2
        ) if counts.get("VISIT", 0) > 0 else 0,
    }


def record_revenue_event(
    db: Session,
    user_id: str,
    event_type: str,
    amount_cents: int,
    product_slug: Optional[str] = None,
    stripe_invoice_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> RevenueEvent:
    """Record a revenue event."""
    event = RevenueEvent(
        user_id=user_id,
        event_type=event_type,
        amount_cents=amount_cents,
        product_slug=product_slug,
        stripe_invoice_id=stripe_invoice_id,
        stripe_subscription_id=stripe_subscription_id,
        metadata_json=metadata,
    )
    db.add(event)
    db.commit()
    return event


def get_revenue_summary(db: Session, days: int = 30) -> dict:
    """Get revenue summary for the last N days."""
    since = datetime.utcnow() - timedelta(days=days)

    events = db.query(RevenueEvent).filter(RevenueEvent.created_at >= since).all()

    total_revenue = sum(e.amount_cents for e in events if e.event_type in ("NEW_MRR", "EXPANSION", "ONE_TIME", "REACTIVATION"))
    total_churn = sum(e.amount_cents for e in events if e.event_type == "CHURN")

    by_product = {}
    for e in events:
        slug = e.product_slug or "unknown"
        if slug not in by_product:
            by_product[slug] = {"count": 0, "total_cents": 0}
        by_product[slug]["count"] += 1
        by_product[slug]["total_cents"] += e.amount_cents

    return {
        "period_days": days,
        "total_revenue_cents": total_revenue,
        "total_churn_cents": total_churn,
        "net_revenue_cents": total_revenue - total_churn,
        "transaction_count": len(events),
        "by_product": by_product,
    }


def compute_monthly_snapshot(db: Session, month: str) -> SubscriptionSnapshot:
    """Compute or update monthly subscription snapshot. Format: '2026-03'."""
    existing = db.query(SubscriptionSnapshot).filter(SubscriptionSnapshot.month == month).first()

    # Get events for this month
    year, m = month.split("-")
    start = datetime(int(year), int(m), 1)
    if int(m) == 12:
        end = datetime(int(year) + 1, 1, 1)
    else:
        end = datetime(int(year), int(m) + 1, 1)

    events = db.query(RevenueEvent).filter(
        RevenueEvent.created_at >= start,
        RevenueEvent.created_at < end,
    ).all()

    new_mrr = sum(e.amount_cents for e in events if e.event_type == "NEW_MRR")
    expansion = sum(e.amount_cents for e in events if e.event_type == "EXPANSION")
    contraction = sum(e.amount_cents for e in events if e.event_type == "CONTRACTION")
    churn = sum(e.amount_cents for e in events if e.event_type == "CHURN")

    if existing:
        existing.new_mrr_cents = new_mrr
        existing.expansion_cents = expansion
        existing.contraction_cents = contraction
        existing.churn_cents = churn
        existing.total_mrr_cents = new_mrr + expansion - contraction - churn
        existing.computed_at = datetime.utcnow()
        db.commit()
        return existing

    snapshot = SubscriptionSnapshot(
        month=month,
        total_mrr_cents=new_mrr + expansion - contraction - churn,
        new_mrr_cents=new_mrr,
        expansion_cents=expansion,
        contraction_cents=contraction,
        churn_cents=churn,
        computed_at=datetime.utcnow(),
    )
    db.add(snapshot)
    db.commit()
    return snapshot
