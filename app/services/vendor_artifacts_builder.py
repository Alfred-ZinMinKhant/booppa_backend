"""Data-assembly for the verifiable offline artefacts.

The PDF *rendering* lives in `vendor_artifacts_generator.py` (pure, no DB). This
module does the per-vendor data assembly (snapshot lookups, profile-view counts,
GeBIZ aggregation) and returns `(filename, pdf_bytes)`. It is the single source of
truth shared by:
  * the on-demand endpoints in `app/api/vendor_artifacts.py`
  * the vendor digest email (`vendor_active_health_check_task`) which attaches them.
"""
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.models import User
from app.services.vendor_artifacts_generator import (
    generate_badge_certificate_pdf,
    generate_priority_placement_pdf,
    generate_competitor_signals_pdf,
    generate_bid_timing_pdf,
)

logger = logging.getLogger(__name__)


def company_of(user: User) -> str:
    return (getattr(user, "company", "") or "").strip() or "Your Company"


def plan_label(user: User) -> str:
    plan = (getattr(user, "plan", "") or "").lower()
    if plan in ("vendor_pro", "vendor_pro_monthly", "vendor_pro_annual"):
        return "Vendor Pro"
    if plan in ("vendor_active", "vendor_active_monthly", "vendor_active_annual"):
        return "Vendor Active"
    return "Vendor"


def build_badge_certificate(db: Session, user: User, company_override: str | None = None) -> tuple[str, bytes]:
    from app.core.models_v8 import VendorStatusSnapshot

    snap = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == user.id)
        .first()
    )
    verify_base = (getattr(settings, "VERIFY_BASE_URL", "https://www.booppa.io") or "https://www.booppa.io").rstrip("/")
    pdf = generate_badge_certificate_pdf({
        "company_name": (company_override or "").strip() or company_of(user),
        "verification_depth": getattr(snap, "verification_depth", None) or "BASIC",
        "procurement_readiness": getattr(snap, "procurement_readiness", None) or "CONDITIONAL",
        "confidence_score": getattr(snap, "confidence_score", None),
        "vendor_id": str(user.id),
        "verify_url": f"{verify_base}/verify/{user.id}",
    })
    return "BOOPPA-Badge-Certificate.pdf", pdf


def build_priority_placement(db: Session, user: User, company_override: str | None = None) -> tuple[str, bytes]:
    from app.core.models import VerifyRecord, ProofView
    from app.core.models_v8 import VendorStatusSnapshot

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    profile_views = 0
    if verify:
        profile_views = (
            db.query(ProofView)
            .filter(ProofView.verify_id == verify.id, ProofView.created_at >= thirty_days_ago)
            .count()
        )
    snap = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == user.id)
        .first()
    )
    label = plan_label(user)
    pdf = generate_priority_placement_pdf({
        "company_name": (company_override or "").strip() or company_of(user),
        "plan_label": label,
        "profile_views_30d": profile_views,
        "verification_depth": getattr(snap, "verification_depth", None) or "BASIC",
        "placement_active": label in ("Vendor Active", "Vendor Pro"),
    })
    return "BOOPPA-Priority-Placement-Report.pdf", pdf


def _vendor_sectors(db: Session, vendor_id) -> list[str]:
    """The vendor's registered sectors (lowercased), or [] if none set."""
    from app.core.models import VendorSector

    rows = db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).all()
    return [r.sector.strip().lower() for r in rows if (r.sector or "").strip()]


def build_bid_timing(db: Session, user: User, months_back: int = 12, company_override: str | None = None) -> tuple[str, bytes]:
    from sqlalchemy import func

    from app.core.models_gebiz import GebizAwardHistory

    since = (datetime.now(timezone.utc) - timedelta(days=30 * months_back)).date()
    q = (
        db.query(GebizAwardHistory)
        .filter(GebizAwardHistory.awarded_date != None, GebizAwardHistory.awarded_date >= since)  # noqa: E711
    )
    # Sector-relevant intelligence: an IT vendor should not receive a report
    # dominated by Facilities/Construction awards. Filter to the vendor's
    # registered sector(s) when available; otherwise show the full market.
    sectors = _vendor_sectors(db, user.id)
    sector_scoped = False
    if sectors:
        scoped = q.filter(func.lower(GebizAwardHistory.sector).in_(sectors))
        if scoped.count() > 0:  # don't blank the report if the sector has no awards
            q = scoped
            sector_scoped = True
    rows = q.all()

    buckets: "OrderedDict[str, dict]" = OrderedDict()
    for r in sorted(rows, key=lambda x: x.awarded_date):
        key = r.awarded_date.strftime("%b %Y")
        b = buckets.setdefault(key, {"month": key, "awards": 0, "value": 0.0})
        b["awards"] += 1
        try:
            b["value"] += float(r.award_amt or 0)
        except (TypeError, ValueError):
            pass

    months = list(buckets.values())
    busiest = max(months, key=lambda m: m["awards"])["month"] if months else "—"
    scope = f"{sectors[0].title()} sector" if sector_scoped else "all sectors"
    period_label = (
        f"GeBIZ awards ({scope}), {months[0]['month']} – {months[-1]['month']}"
        if months else f"GeBIZ award history ({scope})"
    )
    pdf = generate_bid_timing_pdf({
        "company_name": (company_override or "").strip() or company_of(user),
        "period_label": period_label,
        "total_awards": len(rows),
        "busiest_month": busiest,
        "months": months,
    })
    return "BOOPPA-Bid-Timing-Report.pdf", pdf


def build_competitor_signals(
    db: Session, user: User, tender_no: str, window_days: int = 30
) -> tuple[str, bytes]:
    # Reuse the live competitor-signals computation so the PDF matches the dashboard.
    from app.api.vendor_pro import competitor_signals as _live_signals

    signals = _live_signals(tenderNo=tender_no, window_days=window_days, db=db, user=user)
    pdf = generate_competitor_signals_pdf({
        "company_name": company_of(user),
        "tender_no": signals.get("tender_no"),
        "window_days": signals.get("window_days"),
        "lookups": signals.get("lookups"),
        "sector": signals.get("sector"),
        "sector_active_verified": signals.get("sector_active_verified"),
    })
    return "BOOPPA-Competitor-Activity-Report.pdf", pdf
