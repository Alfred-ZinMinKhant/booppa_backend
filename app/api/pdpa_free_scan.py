"""
PDPA Free Scan API
==================
POST /api/v1/pdpa/free-scan — lightweight compliance scan (no AI, no payment)
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.pdpa_free_scan_service import run_free_scan

import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter()

# Simple in-memory rate limit: max 5 scans per IP per hour
_rate_cache: dict[str, list[float]] = {}
RATE_LIMIT = 5
RATE_WINDOW = 3600  # seconds


class FreeScanRequest(BaseModel):
    website_url: str
    email: Optional[str] = None
    company_name: Optional[str] = None


@router.post("/free-scan")
def pdpa_free_scan(body: FreeScanRequest, request: Request, db: Session = Depends(get_db)):
    """Run a free lightweight PDPA compliance scan."""
    # Rate limit by IP
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc).timestamp()
    hits = _rate_cache.get(client_ip, [])
    hits = [t for t in hits if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many scans. Try again later.")
    hits.append(now)
    _rate_cache[client_ip] = hits

    # Validate URL
    url = body.website_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="website_url is required")

    # Run the scan
    result = run_free_scan(url)

    # Save lead if email provided
    if body.email:
        try:
            from app.core.models_v6 import LeadCapture
            lead = LeadCapture(
                id=uuid.uuid4(),
                email=body.email,
                company=body.company_name,
                correlation_id=f"pdpa_free_scan:{url}",
                created_at=datetime.now(timezone.utc),
            )
            db.add(lead)
            db.commit()
        except Exception as e:
            logger.warning(f"[PDPA Free Scan] Lead capture failed: {e}")
            db.rollback()

    return result
