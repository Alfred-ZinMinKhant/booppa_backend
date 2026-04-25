"""
Vendor Dashboard Alerts — consolidated endpoint
=================================================
GET /api/v1/vendor/dashboard-alerts

Returns all vendor state needed by the frontend alert engine:
  - profile (name, UEN, plan, sector)
  - trust score + verification depth
  - PDPA scan status
  - notarization count
  - RFP count
  - sector competitive data (percentile, open tenders, elevated peers)
  - view counts (enterprise, gov)
  - subscription list
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/dashboard-alerts")
async def dashboard_alerts(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    vendor_id = current_user.id
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

    # ── 1. Profile ───────────────────────────────────────────────────────────
    name = (
        getattr(current_user, "company", None)
        or getattr(current_user, "full_name", None)
        or "Vendor"
    )
    uen = getattr(current_user, "uen", None)
    plan = getattr(current_user, "plan", "free") or "free"
    sector = getattr(current_user, "industry", None)

    # ── 2. Trust score ───────────────────────────────────────────────────────
    from app.core.models_v6 import VendorScore

    score_row = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
    trust_score = score_row.total_score if score_row else 0

    # ── 3. Verification depth ────────────────────────────────────────────────
    from app.core.models_v8 import VendorStatusSnapshot

    snapshot = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.vendor_id == vendor_id)
        .first()
    )
    verification_depth = snapshot.verification_depth if snapshot else "UNVERIFIED"

    # ── 4. PDPA scan status ──────────────────────────────────────────────────
    from app.core.models import Report

    pdpa_report = (
        db.query(Report)
        .filter(
            Report.owner_id == vendor_id,
            Report.framework.in_(
                ["pdpa_quick_scan", "pdpa_basic", "pdpa_pro", "pdpa_snapshot"]
            ),
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc())
        .first()
    )
    pdpa_last_scan = (
        pdpa_report.completed_at.isoformat()
        if pdpa_report and pdpa_report.completed_at
        else None
    )

    # ── 5. Notarization count ────────────────────────────────────────────────
    from app.core.models_v6 import VerifyRecord, Proof, ProofView

    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
    notarization_count = 0
    if verify:
        notarization_count = (
            db.query(Proof).filter(Proof.verify_id == verify.id).count()
        )

    # ── 6. RFP count (completed evidence packages) ───────────────────────────
    from app.core.models_v8 import EvidencePackage

    rfp_count = (
        db.query(EvidencePackage)
        .filter(
            EvidencePackage.vendor_id == vendor_id,
            EvidencePackage.status == "READY",
        )
        .count()
    )

    # ── 7. Sector data ──────────────────────────────────────────────────────
    from app.core.models import VendorSector

    sector_row = (
        db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).first()
    )
    primary_sector = sector_row.sector if sector_row else sector

    # Sector percentile from score snapshot
    from app.core.models_v8 import ScoreSnapshot

    latest_score_snap = (
        db.query(ScoreSnapshot)
        .filter(ScoreSnapshot.vendor_id == vendor_id)
        .order_by(ScoreSnapshot.snapshot_at.desc())
        .first()
    )
    sector_percentile = (
        latest_score_snap.sector_percentile
        if latest_score_snap and latest_score_snap.sector_percentile
        else 0
    )

    # Open tenders in sector
    from app.core.models_gebiz import GebizTender

    now = datetime.now(timezone.utc)
    open_tenders = (
        db.query(func.count(GebizTender.id))
        .filter(
            GebizTender.status == "Open",
            (GebizTender.closing_date == None) | (GebizTender.closing_date >= now),
        )
        .scalar()
        or 0
    )

    # Narrow to sector if possible
    if primary_sector:
        try:
            from app.services.tender_service import _CATEGORY_TO_SECTOR

            matching_categories = [
                cat for cat, sec in _CATEGORY_TO_SECTOR.items() if sec == primary_sector
            ]
            if matching_categories:
                category_matches = (
                    db.query(func.count(GebizTender.id))
                    .filter(
                        GebizTender.status == "Open",
                        (GebizTender.closing_date == None)
                        | (GebizTender.closing_date >= now),
                        GebizTender.raw_data["category"].astext.in_(
                            matching_categories
                        ),
                    )
                    .scalar()
                    or 0
                )
                if category_matches > 0:
                    open_tenders = category_matches
        except Exception:
            pass

    # ── 8. Competitor / elevation data ───────────────────────────────────────
    elevated_peers = 0
    competitor_elevated = False
    if primary_sector:
        try:
            from app.services.sector_pressure import get_sector_competitive_pressure

            pressure = get_sector_competitive_pressure(
                db, primary_sector, str(vendor_id)
            )
            elevated_peers = pressure.get("totalElevated", 0)
            competitor_elevated = elevated_peers > 0
        except Exception:
            pass

    # ── 9. View counts (7d) ──────────────────────────────────────────────────
    enterprise_views_7d = 0
    gov_views_7d = 0
    _GOV_KEYWORDS = (
        ".gov.sg",
        ".gov",
        "gebiz",
        "iras",
        "mof.",
        "mti.",
        "defence.",
        "mindef",
    )

    if verify:
        enterprise_views_7d = (
            db.query(func.count(func.distinct(ProofView.domain)))
            .filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= cutoff_7d,
                ProofView.domain.isnot(None),
            )
            .scalar()
            or 0
        )

        gov_domains = (
            db.query(ProofView.domain)
            .filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= cutoff_7d,
            )
            .distinct()
            .all()
        )
        gov_set = set()
        for (domain,) in gov_domains:
            d = (domain or "").lower()
            if any(k in d for k in _GOV_KEYWORDS):
                gov_set.add(domain)
        gov_views_7d = len(gov_set)

    # ── 10. Active subscriptions ─────────────────────────────────────────────
    subscriptions = []
    sub_tier = getattr(current_user, "subscription_tier", None)
    if sub_tier:
        subscriptions.append({"tier": sub_tier})

    # If we have Stripe customer id, fetch active subscription details to show in dashboard
    try:
        from app.core.config import settings
        import os

        stripe_key = settings.STRIPE_SECRET_KEY
        cust_id = getattr(current_user, "stripe_customer_id", None)
        if stripe_key and cust_id:
            import stripe

            stripe.api_key = stripe_key
            subs = stripe.Subscription.list(customer=cust_id, limit=10)
            for s in getattr(subs, "data", subs.get("data", [])):
                status = s.get("status")
                # Only include active/trialing/canceled (with period_end) info
                item = {
                    "id": s.get("id"),
                    "status": status,
                    "current_period_end": None,
                    "price_id": None,
                    "plan": None,
                }
                period = s.get("current_period_end") or s.get("current_period_start")
                if period:
                    try:
                        item["current_period_end"] = datetime.fromtimestamp(
                            int(period), timezone.utc
                        ).isoformat()
                    except Exception:
                        item["current_period_end"] = str(period)
                # extract price/plan
                items = (
                    s.get("items", {}).get("data", [])
                    if isinstance(s.get("items"), dict)
                    else s.get("items")
                )
                if items and len(items) > 0:
                    p = items[0].get("price") or items[0].get("plan")
                    if isinstance(p, dict):
                        item["price_id"] = p.get("id")
                        item["plan"] = p.get("product") or p.get("nickname")
                subscriptions.append(item)
    except Exception:
        # Non-fatal: if Stripe is unavailable, return whatever local subscription_tier exists
        pass

    # Ensure we include any local `Subscription` rows (DB canonical) and expose price_id
    try:
        from app.core.models import Subscription as LocalSubscription

        cust_id = getattr(current_user, "stripe_customer_id", None)
        local_query = db.query(LocalSubscription).filter(
            (LocalSubscription.user_id == vendor_id)
            | (LocalSubscription.stripe_customer_id == cust_id)
        )
        for row in local_query.all():
            # avoid duplicating entries already added from Stripe
            if any(
                x.get("id") == row.stripe_subscription_id
                for x in subscriptions
                if x.get("id")
            ):
                continue
            meta = row.metadata_json or {}
            price_id = None
            # metadata may store price_id or price or items
            if isinstance(meta, dict):
                price_id = (
                    meta.get("price_id") or meta.get("price") or meta.get("priceId")
                )
                # sometimes metadata may embed a plan/product field
                if not price_id:
                    price_id = meta.get("plan") or meta.get("product")

            subscriptions.append(
                {
                    "id": row.stripe_subscription_id,
                    "status": row.status,
                    "current_period_end": (
                        row.current_period_end.isoformat()
                        if row.current_period_end
                        else None
                    ),
                    "price_id": price_id,
                    "plan": row.product_type,
                }
            )
    except Exception:
        # best-effort only
        pass

    return {
        "name": name,
        "uen": uen,
        "plan": plan,
        "trustScore": trust_score,
        "verificationDepth": verification_depth,
        "pdpaLastScan": pdpa_last_scan,
        "notarizationCount": notarization_count,
        "rfpCount": rfp_count,
        "lastTenderCheck": None,
        "sectorPercentile": sector_percentile,
        "sector": primary_sector or "General",
        "openTenders": open_tenders,
        "govViews7d": gov_views_7d,
        "enterpriseViews7d": enterprise_views_7d,
        "competitorElevated": competitor_elevated,
        "elevatedPeers": elevated_peers,
        "daysToRenewal": None,
        "subscriptions": subscriptions,
    }
