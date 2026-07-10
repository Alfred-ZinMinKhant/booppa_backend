from __future__ import annotations
from app.core.route_classes import RetryAPIRoute
"""
PDPA Monitor / Compliance Dashboard — API
=========================================
  GET  /api/v1/pdpa/dashboard  → aggregate dashboard payload (trend, open
                                 findings with aging, drift events, scan history)
  POST /api/v1/pdpa/rescan     → on-demand re-scan for active subscribers
                                 (rate-limited to once per 24h)

Gated by `require_pdpa_access` — active PDPA Monitor *or* Vendor Pro plan.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import Report, User
from app.api.vendor_pro import require_pdpa_access
from app.services.pdpa_dashboard_service import build_pdpa_dashboard

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RetryAPIRoute)

_PDPA_FRAMEWORKS = ("pdpa_quick_scan", "pdpa_snapshot")
_RESCAN_COOLDOWN_HOURS = 24


@router.get("/dashboard")
def pdpa_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(require_pdpa_access),
):
    """Everything the in-app PDPA Monitor dashboard needs, in one call."""
    return build_pdpa_dashboard(db, user.id)


@router.post("/rescan")
def pdpa_rescan(
    db: Session = Depends(get_db),
    user: User = Depends(require_pdpa_access),
):
    """Queue an on-demand PDPA re-scan for the caller's website.

    Reuses the same `pdpa_monitor_monthly_rescan_task` the monthly cron fires,
    so on-demand and scheduled scans are identical. Rate-limited to once per
    24 hours per vendor to prevent abuse.
    """
    website = (getattr(user, "website", "") or "").strip()
    if not website:
        # Fall back to the website on the most recent scan, if any.
        last = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework.in_(_PDPA_FRAMEWORKS))
            .order_by(Report.created_at.desc())
            .first()
        )
        website = (getattr(last, "company_website", "") or "").strip() if last else ""
    if not website:
        raise HTTPException(
            status_code=422,
            detail="No website on file to scan. Add your website in your profile first.",
        )

    # Cooldown: block if a PDPA scan was started in the last 24h.
    cutoff = datetime.utcnow() - timedelta(hours=_RESCAN_COOLDOWN_HOURS)
    recent = (
        db.query(Report)
        .filter(
            Report.owner_id == user.id,
            Report.framework.in_(_PDPA_FRAMEWORKS),
            Report.created_at >= cutoff,
        )
        .order_by(Report.created_at.desc())
        .first()
    )
    if recent:
        raise HTTPException(
            status_code=429,
            detail="A PDPA scan was already started in the last 24 hours. Please try again later.",
        )

    from app.workers.tasks import pdpa_monitor_monthly_rescan_task

    pdpa_monitor_monthly_rescan_task.delay(str(user.id), user.email, website)
    logger.info("[PDPADashboard] on-demand re-scan queued for vendor %s (%s)", user.id, website)
    return {"queued": True, "website": website}
