"""
Tender Intelligence API — Standalone subscription product.

Three subscriber-only endpoints over the existing GeBIZ dataset:
  GET /sector-trends         — win-rate by agency × sector × contract-size band
  GET /awards                — paginated historical award lookup
  GET /timing/{tender_no}    — bid/watch/pass recommendation for a live tender

Gated via require_tender_intelligence — accepts the dedicated plan or any
superset plan (enterprise_pro, pro_suite).
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User
from app.core.models_gebiz import GebizAwardHistory
from app.core.models_v10 import TenderShortlist
from app.billing.enforcement import TENDER_INTELLIGENCE_PLAN_KEYS
from app.services.tender_service import compute_tender_win_probability

logger = logging.getLogger(__name__)

router = APIRouter()


# Win-probability thresholds for bid/watch/pass recommendation.
# Tuned against existing tender_service base_rate distribution (DEFAULT 0.20,
# clamped [0.05, 0.60]). Confirm with stakeholder before launch.
_BID_THRESHOLD = 35.0   # win probability >= 35% → bid
_WATCH_THRESHOLD = 15.0  # 15-35% → watch


def require_tender_intelligence(
    user: User = Depends(get_current_user),
) -> User:
    """Gate dependency: require an active Tender Intelligence subscription
    (or any superset plan)."""
    plan = (getattr(user, "plan", "") or "").lower().strip()
    if plan not in TENDER_INTELLIGENCE_PLAN_KEYS:
        raise HTTPException(
            status_code=403,
            detail="Tender Intelligence subscription required. Visit /pricing/tender-intelligence to subscribe.",
        )
    return user


def _classify_amount_band(amt: Optional[float]) -> str:
    if amt is None:
        return "unknown"
    if amt < 50_000:
        return "<50k"
    if amt < 250_000:
        return "50k-250k"
    if amt < 1_000_000:
        return "250k-1M"
    if amt < 5_000_000:
        return "1M-5M"
    return "5M+"


@router.get("/sector-trends")
def sector_trends(
    sector: Optional[str] = Query(None, description="Filter to one sector (e.g. IT, CONSTRUCTION)"),
    agency: Optional[str] = Query(None, description="Filter to one procuring entity"),
    months: int = Query(12, ge=1, le=60, description="Rolling window in months"),
    db: Session = Depends(get_db),
    user: User = Depends(require_tender_intelligence),
):
    """Win-rate patterns over a rolling window, segmented by agency,
    sector, and contract-size band."""
    since = date.today() - timedelta(days=months * 30)

    q = db.query(GebizAwardHistory).filter(
        GebizAwardHistory.awarded_date != None,  # noqa: E711
        GebizAwardHistory.awarded_date >= since,
    )
    if sector:
        q = q.filter(func.upper(GebizAwardHistory.sector) == sector.upper().strip())
    if agency:
        q = q.filter(func.upper(GebizAwardHistory.procuring_entity) == agency.upper().strip())

    rows = q.all()

    by_agency: dict[str, dict] = {}
    by_sector: dict[str, dict] = {}
    by_band: dict[str, dict] = {}

    for r in rows:
        amt = float(r.award_amt) if r.award_amt is not None else None
        band = _classify_amount_band(amt)
        ag_key = (r.procuring_entity or "UNKNOWN").upper()
        sec_key = (r.sector or "OTHER").upper()

        for bucket, key in ((by_agency, ag_key), (by_sector, sec_key), (by_band, band)):
            entry = bucket.setdefault(key, {"count": 0, "total_amt": 0.0, "unique_suppliers": set()})
            entry["count"] += 1
            if amt is not None:
                entry["total_amt"] += amt
            if r.supplier_name:
                entry["unique_suppliers"].add(r.supplier_name)

    def _serialise(bucket: dict) -> list[dict]:
        out = []
        for key, v in bucket.items():
            cnt = v["count"]
            uniq = len(v["unique_suppliers"])
            out.append({
                "key": key,
                "awards": cnt,
                "unique_winners": uniq,
                "total_value_sgd": round(v["total_amt"], 2),
                "avg_value_sgd": round(v["total_amt"] / cnt, 2) if cnt else 0.0,
                "concentration": round(uniq / cnt, 3) if cnt else None,
            })
        out.sort(key=lambda x: x["awards"], reverse=True)
        return out

    return {
        "window_months": months,
        "since": since.isoformat(),
        "total_awards": len(rows),
        "by_agency": _serialise(by_agency)[:30],
        "by_sector": _serialise(by_sector),
        "by_contract_size": _serialise(by_band),
    }


@router.get("/awards")
def historical_awards(
    agency: Optional[str] = Query(None),
    supplier: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    min_amt: Optional[float] = Query(None, ge=0),
    max_amt: Optional[float] = Query(None, ge=0),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_tender_intelligence),
):
    """Paginated lookup over historical GeBIZ awards."""
    q = db.query(GebizAwardHistory)
    if agency:
        q = q.filter(func.upper(GebizAwardHistory.procuring_entity).like(f"%{agency.upper().strip()}%"))
    if supplier:
        q = q.filter(func.upper(GebizAwardHistory.supplier_name).like(f"%{supplier.upper().strip()}%"))
    if sector:
        q = q.filter(func.upper(GebizAwardHistory.sector) == sector.upper().strip())
    if min_amt is not None:
        q = q.filter(GebizAwardHistory.award_amt >= min_amt)
    if max_amt is not None:
        q = q.filter(GebizAwardHistory.award_amt <= max_amt)
    if from_date:
        q = q.filter(GebizAwardHistory.awarded_date >= from_date)
    if to_date:
        q = q.filter(GebizAwardHistory.awarded_date <= to_date)

    total = q.count()
    rows = (
        q.order_by(GebizAwardHistory.awarded_date.desc().nullslast())
        .limit(limit)
        .offset(offset)
        .all()
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "tender_no": r.tender_no,
                "awarded_date": r.awarded_date.isoformat() if r.awarded_date else None,
                "supplier_name": r.supplier_name,
                "award_amt_sgd": float(r.award_amt) if r.award_amt is not None else None,
                "tender_description": r.tender_description,
                "procuring_entity": r.procuring_entity,
                "sector": r.sector,
            }
            for r in rows
        ],
    }


@router.get("/supplier-benchmark/{supplier_name}")
def supplier_benchmark(
    supplier_name: str,
    months: int = Query(36, ge=1, le=120, description="Rolling window in months"),
    peer_limit: int = Query(10, ge=1, le=50, description="How many peers to surface for benchmarking"),
    db: Session = Depends(get_db),
    user: User = Depends(require_tender_intelligence),
):
    """Per-supplier benchmarking dashboard.

    Returns the supplier's award history (count, total value, win-rate by
    sector and agency, recent wins, time-series), plus a peer benchmark
    ranking against the top suppliers in the same primary sector.
    """
    since = date.today() - timedelta(days=months * 30)
    needle = supplier_name.upper().strip()

    own_rows = (
        db.query(GebizAwardHistory)
        .filter(
            GebizAwardHistory.awarded_date != None,  # noqa: E711
            GebizAwardHistory.awarded_date >= since,
            func.upper(GebizAwardHistory.supplier_name).like(f"%{needle}%"),
        )
        .all()
    )

    if not own_rows:
        raise HTTPException(
            status_code=404,
            detail=f"No awards found for supplier matching '{supplier_name}' in the last {months} months.",
        )

    # ── Aggregate the focal supplier ───────────────────────────────────────
    total_value = 0.0
    by_sector: dict[str, dict] = {}
    by_agency: dict[str, dict] = {}
    by_band: dict[str, int] = {}
    by_month: dict[str, dict] = {}  # YYYY-MM → {count, value}

    for r in own_rows:
        amt = float(r.award_amt) if r.award_amt is not None else 0.0
        total_value += amt
        sec = (r.sector or "OTHER").upper()
        ag = (r.procuring_entity or "UNKNOWN").upper()
        band = _classify_amount_band(amt if r.award_amt is not None else None)

        for bucket, key in ((by_sector, sec), (by_agency, ag)):
            e = bucket.setdefault(key, {"count": 0, "value": 0.0})
            e["count"] += 1
            e["value"] += amt

        by_band[band] = by_band.get(band, 0) + 1

        if r.awarded_date:
            mkey = r.awarded_date.strftime("%Y-%m")
            m = by_month.setdefault(mkey, {"count": 0, "value": 0.0})
            m["count"] += 1
            m["value"] += amt

    awards_count = len(own_rows)
    primary_sector = max(by_sector.items(), key=lambda kv: kv[1]["count"])[0] if by_sector else None

    # ── Peer benchmark within primary sector ───────────────────────────────
    peers: list[dict] = []
    own_rank = None
    if primary_sector:
        peer_q = (
            db.query(
                GebizAwardHistory.supplier_name.label("name"),
                func.count(GebizAwardHistory.id).label("awards"),
                func.coalesce(func.sum(GebizAwardHistory.award_amt), 0).label("total_value"),
            )
            .filter(
                GebizAwardHistory.awarded_date != None,  # noqa: E711
                GebizAwardHistory.awarded_date >= since,
                func.upper(GebizAwardHistory.sector) == primary_sector,
                GebizAwardHistory.supplier_name != None,  # noqa: E711
            )
            .group_by(GebizAwardHistory.supplier_name)
            .order_by(func.count(GebizAwardHistory.id).desc())
            .all()
        )
        # Find the focal supplier's rank within this peer list (loose match).
        for idx, row in enumerate(peer_q, start=1):
            if needle in (row.name or "").upper():
                own_rank = idx
                break
        peers = [
            {
                "supplier_name": row.name,
                "awards": int(row.awards),
                "total_value_sgd": float(row.total_value or 0),
            }
            for row in peer_q[:peer_limit]
        ]

    # Recent wins (most recent first, capped at 10)
    recent_wins = sorted(
        own_rows,
        key=lambda r: r.awarded_date or date.min,
        reverse=True,
    )[:10]

    # Time series sorted by month
    timeseries = [
        {"month": m, "awards": v["count"], "total_value_sgd": round(v["value"], 2)}
        for m, v in sorted(by_month.items())
    ]

    def _serialise_bucket(bucket: dict) -> list[dict]:
        out = [
            {
                "key": k,
                "awards": v["count"],
                "total_value_sgd": round(v["value"], 2),
                "share_of_awards": round(v["count"] / awards_count, 3) if awards_count else 0.0,
            }
            for k, v in bucket.items()
        ]
        out.sort(key=lambda x: x["awards"], reverse=True)
        return out

    return {
        "supplier_query": supplier_name,
        "window_months": months,
        "since": since.isoformat(),
        "summary": {
            "total_awards": awards_count,
            "total_value_sgd": round(total_value, 2),
            "avg_value_sgd": round(total_value / awards_count, 2) if awards_count else 0.0,
            "primary_sector": primary_sector,
            "distinct_agencies": len(by_agency),
            "distinct_sectors": len(by_sector),
        },
        "by_sector": _serialise_bucket(by_sector),
        "by_agency": _serialise_bucket(by_agency)[:20],
        "by_contract_size": [
            {"band": k, "awards": v, "share_of_awards": round(v / awards_count, 3) if awards_count else 0.0}
            for k, v in sorted(by_band.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "timeseries": timeseries,
        "peer_benchmark": {
            "sector": primary_sector,
            "peer_count": len(peers),
            "supplier_rank_in_sector": own_rank,
            "top_peers": peers,
        },
        "recent_wins": [
            {
                "tender_no": r.tender_no,
                "awarded_date": r.awarded_date.isoformat() if r.awarded_date else None,
                "award_amt_sgd": float(r.award_amt) if r.award_amt is not None else None,
                "procuring_entity": r.procuring_entity,
                "sector": r.sector,
                "tender_description": (r.tender_description or "")[:200],
            }
            for r in recent_wins
        ],
    }


def _linear_forecast(series: list[float], horizon: int) -> tuple[list[float], float]:
    """Ordinary least-squares linear regression over a numeric series.

    Returns (forecast values for the next `horizon` points, R^2 of the fit).
    Falls back to a flat mean when the series is too short or has zero
    variance — keeps the endpoint deterministic without numpy.
    """
    n = len(series)
    if n < 2:
        mean = series[0] if series else 0.0
        return [max(0.0, mean)] * horizon, 0.0

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return [max(0.0, mean_y)] * horizon, 0.0

    slope = num / den
    intercept = mean_y - slope * mean_x

    ss_res = sum((series[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - mean_y) ** 2 for y in series)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    forecast = [
        max(0.0, slope * (n + h) + intercept) for h in range(horizon)
    ]
    return forecast, max(0.0, min(1.0, r2))


@router.get("/forecast")
def forecast(
    sector: Optional[str] = Query(None, description="Filter to one sector"),
    agency: Optional[str] = Query(None, description="Filter to one procuring entity"),
    history_months: int = Query(18, ge=6, le=60, description="History window for the fit"),
    horizon_months: int = Query(3, ge=1, le=12, description="How many months ahead to forecast"),
    db: Session = Depends(get_db),
    user: User = Depends(require_tender_intelligence),
):
    """Project expected awards (count + total value) for the next
    `horizon_months` by linear regression over the historical monthly
    award time-series. Optionally narrowed to a sector or agency.

    Returns the underlying monthly series, the forecast points, and a fit
    quality (R²) so the client can render confidence appropriately.
    """
    today = date.today()
    since = date(today.year, today.month, 1) - timedelta(days=history_months * 31)

    q = db.query(GebizAwardHistory).filter(
        GebizAwardHistory.awarded_date != None,  # noqa: E711
        GebizAwardHistory.awarded_date >= since,
    )
    if sector:
        q = q.filter(func.upper(GebizAwardHistory.sector) == sector.upper().strip())
    if agency:
        q = q.filter(func.upper(GebizAwardHistory.procuring_entity) == agency.upper().strip())

    rows = q.all()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No award history in the requested window for this filter.",
        )

    # Bucket the rows into YYYY-MM
    buckets: dict[str, dict] = {}
    for r in rows:
        if not r.awarded_date:
            continue
        key = r.awarded_date.strftime("%Y-%m")
        b = buckets.setdefault(key, {"count": 0, "value": 0.0})
        b["count"] += 1
        b["value"] += float(r.award_amt) if r.award_amt is not None else 0.0

    # Fill missing months so the regression sees an unbroken series.
    def _month_iter(start: date, end: date):
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            yield f"{y:04d}-{m:02d}"
            m += 1
            if m == 13:
                m, y = 1, y + 1

    months = list(_month_iter(since, today))
    counts = [buckets.get(m, {}).get("count", 0) for m in months]
    values = [buckets.get(m, {}).get("value", 0.0) for m in months]

    fc_counts, r2_counts = _linear_forecast([float(c) for c in counts], horizon_months)
    fc_values, r2_values = _linear_forecast(values, horizon_months)

    # Stamp forecast months onto calendar.
    last_y, last_m = today.year, today.month
    future_months = []
    for h in range(1, horizon_months + 1):
        fm = last_m + h
        fy = last_y + (fm - 1) // 12
        fm = ((fm - 1) % 12) + 1
        future_months.append(f"{fy:04d}-{fm:02d}")

    return {
        "filters": {"sector": sector, "agency": agency},
        "history_months": history_months,
        "horizon_months": horizon_months,
        "model": "ordinary_least_squares_linear",
        "fit_quality": {
            "r2_award_count": round(r2_counts, 3),
            "r2_total_value": round(r2_values, 3),
        },
        "history": [
            {"month": m, "awards": c, "total_value_sgd": round(v, 2)}
            for m, c, v in zip(months, counts, values)
        ],
        "forecast": [
            {
                "month": future_months[i],
                "expected_awards": round(fc_counts[i], 1),
                "expected_total_value_sgd": round(fc_values[i], 2),
            }
            for i in range(horizon_months)
        ],
    }


@router.get("/timing/{tender_no}")
def timing_recommendation(
    tender_no: str,
    vendor_id: Optional[str] = Query(None, description="Vendor UUID for personalised probability"),
    db: Session = Depends(get_db),
    user: User = Depends(require_tender_intelligence),
):
    """Bid / watch / pass recommendation for a live tender, with comparable
    historical awards. Wraps the existing win-probability engine."""
    result = compute_tender_win_probability(db, tender_no, vendor_id)
    if result.get("error") == "tender_not_found":
        raise HTTPException(status_code=404, detail=f"Tender '{tender_no}' not found")

    win_pct = float(result.get("currentProbability") or 0.0)
    if win_pct >= _BID_THRESHOLD:
        recommendation = "bid"
    elif win_pct >= _WATCH_THRESHOLD:
        recommendation = "watch"
    else:
        recommendation = "pass"

    # Confidence = distance from the nearest threshold, scaled. Cheap heuristic;
    # replace with a calibrated curve once we have outcome data per recommendation.
    boundary_distance = min(
        abs(win_pct - _BID_THRESHOLD),
        abs(win_pct - _WATCH_THRESHOLD),
        win_pct,
        100.0 - win_pct,
    )
    confidence = round(min(1.0, boundary_distance / 25.0), 2)

    # Comparable awards: same agency or sector, within last 24 months.
    since = date.today() - timedelta(days=730)
    comparables_q = db.query(GebizAwardHistory).filter(
        GebizAwardHistory.awarded_date >= since,
    )
    agency_val = result.get("agency")
    sector_val = result.get("sector")
    if agency_val:
        comparables_q = comparables_q.filter(
            func.upper(GebizAwardHistory.procuring_entity) == str(agency_val).upper().strip()
        )
    elif sector_val:
        comparables_q = comparables_q.filter(
            func.upper(GebizAwardHistory.sector) == str(sector_val).upper().strip()
        )
    comparables = (
        comparables_q.order_by(GebizAwardHistory.awarded_date.desc().nullslast())
        .limit(10)
        .all()
    )

    return {
        "tender_no": tender_no,
        "recommendation": recommendation,
        "confidence": confidence,
        "win_probability_pct": win_pct,
        "thresholds": {"bid": _BID_THRESHOLD, "watch": _WATCH_THRESHOLD},
        "agency": agency_val,
        "sector": sector_val,
        "comparable_awards": [
            {
                "awarded_date": c.awarded_date.isoformat() if c.awarded_date else None,
                "supplier_name": c.supplier_name,
                "award_amt_sgd": float(c.award_amt) if c.award_amt is not None else None,
                "tender_description": (c.tender_description or "")[:200],
            }
            for c in comparables
        ],
        "raw": result,
    }
