"""
GeBIZ Tenders API
=================
GET /api/gebiz/latest-tenders  — Returns open tenders sorted by closing_date asc.
                                  Authenticated vendors receive a smart_match flag
                                  indicating sector alignment with their profile.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models_gebiz import GebizTender

router = APIRouter()
logger = logging.getLogger(__name__)


class TenderOut(BaseModel):
    tender_no: str
    title: str
    agency: str
    closing_date: Optional[datetime]
    estimated_value: Optional[float]
    status: str
    url: Optional[str]
    last_fetched_at: Optional[datetime]

    class Config:
        from_attributes = True


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    """Returns the current count of Open tenders for production health checks."""
    count = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .count()
    )
    return {"open_tender_count": count}


@router.get("/latest-tenders", response_model=List[TenderOut])
def get_latest_tenders(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Return open GeBIZ tenders sorted by closing date (soonest first).
    Closed or expired tenders are excluded.

    If the database has no open tenders (e.g. Celery Beat hasn't run yet),
    an on-demand sync from GeBIZ RSS feeds is triggered automatically.
    """
    from app.services.gebiz_service import ensure_tenders_loaded

    # On-demand fallback: populate tenders if DB is empty
    try:
        ensure_tenders_loaded(db)
    except Exception as exc:
        logger.warning(f"[GeBIZ] On-demand sync failed in API: {exc}")

    now = datetime.now(timezone.utc)
    tenders = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .filter(
            (GebizTender.closing_date == None) | (GebizTender.closing_date >= now)  # noqa: E711
        )
        .order_by(GebizTender.closing_date.asc().nullslast())
        .limit(limit)
        .all()
    )
    return tenders
