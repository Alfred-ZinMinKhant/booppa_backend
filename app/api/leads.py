from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Dict, Any
from app.core.db import get_db
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/")
def get_leads(db: Session = Depends(get_db)):
    """Returns ranked lead list from hot_leads table"""
    try:
        sql = text("""
            SELECT domain, score, score_delta, summary, sessions_count,
                   last_event, ai_insight, top_vendors, fit_score
            FROM hot_leads
            ORDER BY score DESC
            LIMIT 100
        """)
        result = db.execute(sql)
        leads = [dict(row._mapping) for row in result]
        return leads
    except Exception as e:
        logger.error(f"Failed to fetch leads: {e}")
        return []

@router.get("/stats")
def get_lead_stats(db: Session = Depends(get_db)):
    """Returns dashboard KPI summary"""
    try:
        sql = text("""
            SELECT
                COUNT(*)                           AS total_leads,
                COUNT(*) FILTER (WHERE score > 30) AS hot_leads_count,
                COUNT(*) FILTER (WHERE score_delta > 0) AS rising,
                AVG(score)                         AS avg_score,
                MAX(updated_at)                    AS last_run
            FROM hot_leads
        """)
        row = db.execute(sql).fetchone()
        if not row:
            return {"total_leads": 0, "hot_leads": 0, "rising": 0, "avg_score": 0, "last_run": None}
            
        data = dict(row._mapping)
        # Rename hot_leads_count to hot_leads for frontend compatibility if needed
        data["hot_leads"] = data.pop("hot_leads_count")
        return data
    except Exception as e:
        logger.error(f"Failed to fetch lead stats: {e}")
        return {"error": str(e)}

@router.get("/activity")
def get_lead_activity(db: Session = Depends(get_db)):
    """Returns score timeline for sparklines"""
    try:
        sql = text("""
            SELECT domain, score, updated_at
            FROM hot_leads
            WHERE updated_at > NOW() - INTERVAL '7 days'
            ORDER BY updated_at DESC
        """)
        result = db.execute(sql)
        return [dict(row._mapping) for row in result]
    except Exception as e:
        logger.error(f"Failed to fetch activity: {e}")
        return []

@router.get("/{domain}")
def get_lead_detail(domain: str, db: Session = Depends(get_db)):
    """Returns full lead detail for a specific domain"""
    try:
        sql = text("SELECT * FROM hot_leads WHERE domain = :domain")
        row = db.execute(sql, {"domain": domain}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        return dict(row._mapping)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch lead detail for {domain}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
