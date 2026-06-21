"""
Vendor Pro — subscriber-only endpoints.

  GET  /vendor-pro/competitor-signals?tenderNo=  → anonymised lookup signal
  POST /vendor-pro/lookup-opt-out               → toggle per-user opt-out
  GET  /vendor-pro/lookup-opt-out               → read current opt-out state
  GET  /vendor-pro/me                           → quota/scan/opt-out snapshot

Gating: `require_vendor_pro` accepts vendor_pro + any superset plan.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import User
from app.core.models_vendor_pro import TenderCheckLookup
from app.billing.enforcement import VENDOR_PRO_PLAN_KEYS
from app.services.tender_similarity import find_similar_tenders

logger = logging.getLogger(__name__)
router = APIRouter()


def require_vendor_pro(user: User = Depends(get_current_user)) -> User:
    plan = (getattr(user, "plan", "") or "").lower().strip()
    if plan not in VENDOR_PRO_PLAN_KEYS:
        raise HTTPException(
            status_code=403,
            detail="Vendor Pro subscription required. Visit /pricing to subscribe.",
        )
    return user


@router.get("/monthly-report.pdf")
def monthly_report(
    db: Session = Depends(get_db),
    user: User = Depends(require_vendor_pro),
):
    """Stream the consolidated Vendor Pro Monthly Intelligence Report on demand.

    Reuses `build_pro_report_pdf` — the same assembler behind the email digest —
    so the download never diverges from the emailed copy. 403 for non-Pro.
    """
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from app.services.vendor_pro_report_generator import build_pro_report_pdf

    pdf = build_pro_report_pdf(db, str(user.id), company=getattr(user, "company", None))
    safe = (getattr(user, "company", None) or "report").replace("/", "-").replace(" ", "-")
    return StreamingResponse(
        BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="BOOPPA-Vendor-Pro-Report-{safe}.pdf"'},
    )


@router.get("/competitor-signals")
def competitor_signals(
    tenderNo: str = Query(..., description="GeBIZ tender number"),
    window_days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(require_vendor_pro),
):
    """Anonymised lookup activity on this tender + similar tenders.

    Counts only — no vendor identities ever surface in the response.
    """
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    since_naive = since.replace(tzinfo=None)  # column is naive UTC

    # Focal tender counts
    focal_q = db.query(TenderCheckLookup).filter(
        TenderCheckLookup.tender_no == tenderNo,
        TenderCheckLookup.created_at >= since_naive,
    )
    focal_total = focal_q.count()
    focal_verified = focal_q.filter(TenderCheckLookup.is_verified == True).count()  # noqa: E712

    # Similar tenders
    similar_nos = find_similar_tenders(db, tenderNo, limit=20)
    similar_total = 0
    similar_verified = 0
    if similar_nos:
        sim_q = db.query(TenderCheckLookup).filter(
            TenderCheckLookup.tender_no.in_(similar_nos),
            TenderCheckLookup.created_at >= since_naive,
        )
        similar_total = sim_q.count()
        similar_verified = sim_q.filter(TenderCheckLookup.is_verified == True).count()  # noqa: E712

    # Sector match: active verified vendors in this tender's sector who looked at
    # *anything* in that sector recently (loose interest signal).
    sector = None
    if focal_q.first() is not None:
        # Re-query to grab a sector hint
        row = focal_q.order_by(TenderCheckLookup.created_at.desc()).first()
        sector = row.sector if row else None
    sector_active_verified = 0
    if sector:
        sector_active_verified = (
            db.query(func.count(func.distinct(TenderCheckLookup.vendor_id)))
            .filter(
                TenderCheckLookup.sector == sector,
                TenderCheckLookup.is_verified == True,  # noqa: E712
                TenderCheckLookup.created_at >= since_naive,
                TenderCheckLookup.vendor_id != None,  # noqa: E711
            )
            .scalar()
            or 0
        )

    return {
        "tender_no": tenderNo,
        "window_days": window_days,
        "lookups": {
            "focal": focal_total,
            "focal_verified": focal_verified,
            "similar": similar_total,
            "similar_verified": similar_verified,
        },
        "similar_tender_nos": similar_nos,
        "sector": sector,
        "sector_active_verified": int(sector_active_verified),
    }


class OptOutPayload(BaseModel):
    opt_out: bool


@router.post("/lookup-opt-out")
def set_lookup_opt_out(
    body: OptOutPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_vendor_pro),
):
    """Toggle the user's tender_check_lookup opt-out flag."""
    user.tender_lookup_opt_out = bool(body.opt_out)
    db.add(user)
    db.commit()
    return {"opt_out": user.tender_lookup_opt_out}


@router.get("/lookup-opt-out")
def get_lookup_opt_out(user: User = Depends(require_vendor_pro)):
    return {"opt_out": bool(getattr(user, "tender_lookup_opt_out", False))}


@router.get("/me")
def vendor_pro_me(
    db: Session = Depends(get_db),
    user: User = Depends(require_vendor_pro),
):
    """Snapshot for the Vendor Pro dashboard widget: notarization quota,
    last/next PDPA scan dates, opt-out state, recent signal count."""
    from app.core.models_v8 import NotarizationCredit, ENTERPRISE_NOTARIZATION_LIMITS

    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    nc = (
        db.query(NotarizationCredit)
        .filter(
            NotarizationCredit.user_id == user.id,
            NotarizationCredit.month == month_key,
        )
        .first()
    )
    plan = (user.plan or "").lower()
    limit = ENTERPRISE_NOTARIZATION_LIMITS.get(plan, 0)
    used = int(nc.used) if nc else 0

    # Last/next PDPA scan: best-effort lookup of latest PDPA Report row.
    last_pdpa = None
    try:
        from app.core.models import Report
        rep = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                func.lower(Report.framework).like("%pdpa%"),
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        if rep and rep.created_at:
            last_pdpa = rep.created_at.isoformat()
    except Exception:
        pass

    # Next quarterly PDPA: 1st of the next Jan/Apr/Jul/Oct.
    now = datetime.now(timezone.utc)
    quarters = [1, 4, 7, 10]
    upcoming = [q for q in quarters if q > now.month]
    if upcoming:
        next_pdpa = datetime(now.year, upcoming[0], 1, 3, 30, tzinfo=timezone.utc)
    else:
        next_pdpa = datetime(now.year + 1, 1, 1, 3, 30, tzinfo=timezone.utc)

    return {
        "plan": plan,
        "notarization": {"used": used, "limit": limit, "month": month_key},
        "pdpa": {
            "last_scan_at": last_pdpa,
            "next_scan_at": next_pdpa.isoformat(),
        },
        "lookup_opt_out": bool(getattr(user, "tender_lookup_opt_out", False)),
    }
