from app.core.route_classes import RetryAPIRoute
"""
Vendor Status Routes — V8
==========================
GET /api/vendor/status           → full VendorStatusProfile (auth'd vendor)
GET /api/vendor/sector-pressure  → sector competitive pressure snapshot + message
GET /api/vendor/dashboard-cal    → CAL payload: ladder + suggestion + message + sectorPressure
"""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.services.vendor_status import get_vendor_status
from app.services.sector_pressure import (
    get_sector_competitive_pressure,
    generate_sector_pressure_message,
    get_cached_rows,
    count_recently_active,
)
from app.services.cal import (
    analyze_activation_gaps,
    generate_upgrade_suggestion,
    render_message,
)
from app.services.notarization_elevation import fetch_elevation_metadata
from app.core.models import VendorSector

router = APIRouter(route_class=RetryAPIRoute)


@router.get("/status")
async def vendor_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Full VendorStatusProfile for the authenticated vendor.
    Derived from trust facts only — no payment data.
    Auto-refreshes score if monitoring is stale (>7 days).
    """
    from app.core.models import ScoreSnapshot
    vendor_id = str(current_user.id)

    # Auto-refresh stale scores so the dashboard always shows live numbers
    latest_snapshot = (
        db.query(ScoreSnapshot)
        .filter(ScoreSnapshot.vendor_id == vendor_id)
        .order_by(ScoreSnapshot.snapshot_at.desc())
        .first()
    )
    snapshot_age_days = 999
    if latest_snapshot:
        snap_at = latest_snapshot.snapshot_at
        if snap_at.tzinfo is None:
            snap_at = snap_at.replace(tzinfo=timezone.utc)
        snapshot_age_days = (datetime.now(timezone.utc) - snap_at).days
    if snapshot_age_days >= 7:
        try:
            from app.services.scoring import VendorScoreEngine
            VendorScoreEngine.update_vendor_score(db, vendor_id)
        except Exception as _e:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Auto-refresh score failed for vendor=%s: %s", vendor_id, _e
            )

    status = get_vendor_status(db, vendor_id)
    return status


@router.get("/insights")
async def vendor_insights(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Vendor Active/Pro dashboard insights: score trend vs last cycle, sector
    benchmark, and personalised BID/WATCH/PASS tender matches. Reuses the same
    helpers behind the monthly digest + snapshot PDF so all three stay in sync.
    Every block is best-effort and may be null/empty.
    """
    from app.services.vendor_active_insights import (
        get_score_trend, get_sector_benchmark, get_tender_matches,
    )
    from app.billing.enforcement import VENDOR_PRO_PLAN_KEYS
    vendor_id = str(current_user.id)
    is_pro = (getattr(current_user, "plan", "") or "").lower().strip() in VENDOR_PRO_PLAN_KEYS
    matches = get_tender_matches(
        db, vendor_id, limit=8 if is_pro else 5, with_win_probability=is_pro
    )
    return {
        "isPro": is_pro,
        "trend": get_score_trend(db, vendor_id),
        "sectorBenchmark": get_sector_benchmark(db, vendor_id),
        "tenderMatches": [
            {
                "tenderNo": m.get("tender_no"),
                "title": m.get("title"),
                "agency": m.get("agency"),
                "closingDate": m["closing_date"].isoformat() if hasattr(m.get("closing_date"), "isoformat") else None,
                "url": m.get("url"),
                "label": m.get("bid_label"),
                "reason": m.get("bid_reason"),
                "confidence": m.get("bid_confidence"),
                "winProbability": m.get("win_probability"),
                "winLikelihoodTier": m.get("win_likelihood_tier"),
            }
            for m in matches
        ],
    }


@router.get("/sector-pressure")
async def sector_pressure(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns the vendor's competitive snapshot in their primary sector,
    plus an informational message.
    Read-only — no ranking modification.
    """
    vendor_id = str(current_user.id)

    # Resolve primary sector (1 DB query)
    sector_row = db.query(VendorSector).filter(
        VendorSector.vendor_id == current_user.id
    ).first()

    if not sector_row:
        return {
            "snapshot": {
                "sector": None,
                "totalInSector": 0,
                "totalElevated": 0,
                "elevationRate": 0.0,
                "vendorRank": None,
                "avgEvidence": 0.0,
                "vendorEvidence": 0,
            },
            "message": "Register your sector to see how you compare with peers in your industry.",
        }

    primary_sector = sector_row.sector

    snapshot          = get_sector_competitive_pressure(db, primary_sector, vendor_id)
    cached_rows       = get_cached_rows(primary_sector)
    recently_active   = count_recently_active(cached_rows, 30)
    message           = generate_sector_pressure_message(snapshot, recently_active)

    return {"snapshot": snapshot, "message": message}


_EMPTY_SECTOR_PRESSURE = {
    "sector": "General",
    "totalInSector": 0,
    "totalElevated": 0,
    "elevationRate": 0.0,
    "vendorRank": None,
    "avgEvidence": 0.0,
    "vendorEvidence": 0,
}


@router.get("/dashboard-cal")
async def dashboard_cal(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Commercial Activation Layer (CAL) payload for the vendor dashboard:
      - activation ladder (which levels are met, what is next)
      - upgrade suggestion (probability score + insight)
      - dynamic message (gap × sector density × tier matrix)
      - sector pressure context
    """
    vendor_id = str(current_user.id)

    # Single DB lookup: sector + elevation
    sector_row = db.query(VendorSector).filter(
        VendorSector.vendor_id == current_user.id
    ).first()

    primary_sector = sector_row.sector if sector_row else None

    # Elevation metadata & vendor score data
    elevation = fetch_elevation_metadata(db, vendor_id)

    from app.core.models import VendorScore
    score_row = db.query(VendorScore).filter(
        VendorScore.vendor_id == current_user.id
    ).first()

    vendor_snapshot = {
        "vendorId":        vendor_id,
        "compliance_score":  score_row.compliance_score if score_row else 0,
        "evidence_count":    elevation.get("evidence_count", 0),
        "confidence_score":  elevation.get("confidence_score", 0.0),
        "is_elevated":       elevation.get("structural_level") == "ELEVATED",
        "plan":              getattr(current_user, "role", "VENDOR"),
        "tier":              "STANDARD",
    }

    # Sector pressure — empty if no sector registered yet
    if primary_sector:
        sector_pressure = get_sector_competitive_pressure(db, primary_sector, vendor_id)
        cached_rows     = get_cached_rows(primary_sector)
        recently_active = count_recently_active(cached_rows, 30)
    else:
        sector_pressure = _EMPTY_SECTOR_PRESSURE
        cached_rows     = []
        recently_active = 0

    # Peer evidence top-3 avg
    peer_evidences = sorted(
        [
            r.get("evidence_count", 0)
            for r in cached_rows
            if str(r.get("vendor_id")) != vendor_id
        ],
        reverse=True,
    )
    top3 = peer_evidences[:3]
    top3_avg = round(sum(top3) / len(top3), 1) if top3 else 0.0

    # CAL pure functions
    gap_analysis = analyze_activation_gaps(vendor_snapshot, sector_pressure)
    suggestion   = generate_upgrade_suggestion(
        vendor_snapshot,
        gap_analysis,
        sector_pressure["totalElevated"],
    )
    message = render_message({
        "vendor":               vendor_snapshot,
        "sector":               primary_sector,
        "peerAvgEvidence":      sector_pressure["avgEvidence"],
        "vendorEvidence":       sector_pressure["vendorEvidence"],
        "top3AvgEvidence":      top3_avg,
        "recentlyActiveCount":  recently_active,
        "totalElevatedPeers":   sector_pressure["totalElevated"],
        "gapAnalysis":          gap_analysis,
        "suggestion":           suggestion,
    })

    return {
        "ladder":         gap_analysis,
        "suggestion":     suggestion,
        "message":        message,
        "sectorPressure": sector_pressure,
    }


@router.get("/badge")
async def vendor_badge(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns the embeddable HTML badge snippet for the authenticated vendor.
    Only available when VerifyRecord is ACTIVE.
    """
    from app.core.models import VerifyRecord, LifecycleStatus
    from app.core.models import MarketplaceVendor
    from app.core.config import settings

    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == current_user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
    ).first()

    if not verify:
        return {"active": False, "html": None, "profile_url": None}

    # Resolve slug from marketplace vendor record
    mv = db.query(MarketplaceVendor).filter(
        MarketplaceVendor.claimed_by_user_id == current_user.id
    ).first()

    base_url = getattr(settings, "VERIFY_BASE_URL", "https://www.booppa.io")
    slug = mv.slug if mv else str(current_user.id)
    profile_url = f"{base_url}/vendors/{slug}"
    badge_img   = f"{base_url}/booppa-verified-badge.svg"

    html = (
        f'<a href="{profile_url}" target="_blank" rel="noopener">'
        f'<img src="{badge_img}" alt="Verified on BOOPPA" width="160" height="48" />'
        f'</a>'
    )

    from app.core.models import VendorScore
    score_row = db.query(VendorScore).filter(
        VendorScore.vendor_id == current_user.id
    ).first()
    compliance_score = (
        score_row.compliance_score
        if score_row and score_row.compliance_score
        else verify.compliance_score
    )

    # Expiry / renewal — the certificate is time-boxed (12 months). Surfacing
    # days-remaining drives in-app renewal before the public /verify page flips
    # to "Expired" (which a procurement officer would see).
    expires_at = verify.expires_at
    days_remaining = None
    if expires_at:
        exp = expires_at.replace(tzinfo=None) if getattr(expires_at, "tzinfo", None) else expires_at
        days_remaining = (exp - datetime.utcnow()).days

    return {
        "active":       True,
        "html":         html,
        "profile_url":  profile_url,
        "slug":         slug,
        "compliance_score": compliance_score,
        "verification_level": verify.verification_level.value if hasattr(verify.verification_level, 'value') else str(verify.verification_level),
        "expires_at":   expires_at.isoformat() if expires_at else None,
        "days_remaining": days_remaining,
        "last_refreshed_at": verify.last_refreshed_at.isoformat() if verify.last_refreshed_at else None,
    }


# ── Evidence Management ───────────────────────────────────────────────────────

from fastapi import UploadFile, File
from app.core.models import Proof, VerifyRecord, LifecycleStatus, VerificationLevel
import hashlib

@router.get("/evidence")
async def list_evidence(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all notarized evidence for the authenticated vendor."""
    # Find active verify record
    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == current_user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE
    ).first()
    
    if not verify:
        return []
        
    proofs = db.query(Proof).filter(Proof.verify_id == verify.id).order_by(Proof.created_at.desc()).all()
    
    return [
        {
            "id": str(p.id),
            "filename": p.title or "Document",
            "hash": p.hash_value,
            "blockchain_tx": p.metadata_json.get("tx_hash") if p.metadata_json else None,
            "verify_url": p.metadata_json.get("verify_url") if p.metadata_json else None,
            "created_at": p.created_at.isoformat()
        }
        for p in proofs
    ]


@router.post("/evidence")
async def upload_evidence(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Upload a document, notarize it, and anchor to blockchain."""
    # 1. Get or create VerifyRecord
    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == current_user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE
    ).first()
    
    if not verify:
        verify = VerifyRecord(
            vendor_id=current_user.id,
            verification_level=VerificationLevel.BASIC,
            compliance_score=0
        )
        db.add(verify)
        db.commit()
        db.refresh(verify)
        
    # 2. Read file and hash it
    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    
    # Check for duplicate hash
    existing = db.query(Proof).filter(Proof.hash_value == file_hash).first()
    if existing:
        raise HTTPException(status_code=409, detail="This document has already been notarized.")
        
    # 3. Anchor to blockchain (Polygon Amoy Testnet)
    tx_hash = None
    anchor_status = "pending_anchor"
    try:
        from app.services.blockchain import BlockchainService
        blockchain = BlockchainService()
        tx_hash = await blockchain.anchor_evidence(
            file_hash, metadata=f"vendor_evidence:vendor:{current_user.id}"
        )
        anchor_status = "anchored"
    except Exception as exc:
        logger.warning("Blockchain anchor failed for evidence upload (will retry later): %s", exc)

    explorer_base = settings.active_polygon_explorer_url.rstrip("/")
    verify_url = (
        f"{explorer_base}/tx/{tx_hash}"
        if tx_hash else None
    )

    # 4. Create Proof record
    proof = Proof(
        verify_id=verify.id,
        hash_value=file_hash,
        title=file.filename,
        metadata_json={
            "size": len(content),
            "content_type": file.content_type,
            "status": anchor_status,
            "tx_hash": tx_hash,
            "verify_url": verify_url,
            "network": settings.active_polygon_network_name,
            "testnet_notice": settings.blockchain_notice,
        }
    )
    db.add(proof)

    # 5. Update vendor score visibility bonus
    from app.services.scoring import VendorScoreEngine
    db.commit()  # save proof first
    VendorScoreEngine.update_vendor_score(db, str(current_user.id))

    db.refresh(proof)
    return {
        "id": str(proof.id),
        "filename": proof.title,
        "hash": proof.hash_value,
        "created_at": proof.created_at.isoformat(),
        "tx_hash": proof.metadata_json.get("tx_hash"),
        "verify_url": proof.metadata_json.get("verify_url"),
        "network": settings.active_polygon_network_name,
        "testnet_notice": settings.blockchain_notice,
        "anchor_status": anchor_status,
    }


# ── Profile Management ────────────────────────────────────────────────────────

from pydantic import BaseModel

class ProfileUpdate(BaseModel):
    company: Optional[str] = None
    industry: Optional[str] = None

@router.get("/profile")
async def get_profile(current_user=Depends(get_current_user)):
    """Return the authenticated vendor's profile."""
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "company": getattr(current_user, "company", None),
        "industry": getattr(current_user, "industry", None),
        "role": getattr(current_user, "role", "VENDOR")
    }

@router.patch("/profile")
async def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update vendor profile details."""
    from app.core.models import User
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if body.company is not None:
        user.company = body.company
    if body.industry is not None:
        user.industry = body.industry
    db.commit()

    # Propagate industry to MarketplaceVendor if linked
    if body.industry is not None:
        try:
            from app.core.models import MarketplaceVendor
            mv = db.query(MarketplaceVendor).filter(
                MarketplaceVendor.claimed_by_user_id == user.id
            ).first()
            if mv:
                mv.industry = body.industry
                db.commit()
        except Exception:
            pass

    return {"status": "success", "company": user.company, "industry": user.industry}


# ── Subscription surface ──────────────────────────────────────────────────────


@router.get("/subscription")
async def vendor_subscription(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the vendor's current plan, unlocked features, and quota usage."""
    from datetime import datetime as _dt, timezone as _tz
    from app.billing.enforcement import enforce_tier
    from app.core.models import NotarizationCredit, ENTERPRISE_NOTARIZATION_LIMITS
    from app.services.pricing import get_product

    plan_value = (getattr(current_user, "plan", "free") or "free").lower()

    # Run the same enforcement the rest of the platform uses, so the surfaced
    # feature flags are guaranteed to match what's actually gated server-side.
    result = enforce_tier(
        assessment_data={
            "plan": plan_value,
            "subscription_status": "active" if getattr(current_user, "stripe_subscription_id", None) else "inactive",
            "payment_confirmed": bool(getattr(current_user, "stripe_subscription_id", None)),
        },
        framework=None,
    )
    features = result.get("features") or {}
    quota = int(features.get("monthly_notarization_quota") or 0)

    # Notarization usage for the current month
    month_key = _dt.now(_tz.utc).strftime("%Y-%m")
    nc = (
        db.query(NotarizationCredit)
        .filter(
            NotarizationCredit.user_id == current_user.id,
            NotarizationCredit.month == month_key,
        )
        .first()
    )
    used = int(nc.used) if nc else 0

    # Best-effort marketing label / price from the source-of-truth pricing.py
    monthly_slug = f"{plan_value}_monthly"
    product = get_product(monthly_slug) or get_product(plan_value)
    plan_label = (product or {}).get("name") or plan_value.replace("_", " ").title()
    price_sgd = (product or {}).get("price_sgd")

    # Buyers can hold multiple concurrent Stripe subscriptions (e.g. Vendor
    # Pro + PDPA Monitor + Tender Intelligence). The User row only carries
    # the LATEST plan, so the rest are invisible to the UI unless we surface
    # them explicitly. Pull every non-cancelled Subscription row for this
    # user and decorate with marketing label + price from pricing.py.
    from app.core.models import Subscription as _SubscriptionRow
    sub_rows = (
        db.query(_SubscriptionRow)
        .filter(_SubscriptionRow.user_id == current_user.id)
        .filter(_SubscriptionRow.status.in_(["active", "trialing", "past_due"]))
        .order_by(_SubscriptionRow.created_at.desc())
        .all()
    )
    all_subscriptions: list[dict] = []
    for s in sub_rows:
        product_slug = s.product_type or ""
        prod = get_product(product_slug) or get_product(product_slug.replace("_monthly", "")) or {}
        all_subscriptions.append({
            "id": str(s.id),
            "product_type": product_slug,
            "label": prod.get("name") or product_slug.replace("_", " ").title(),
            "description": prod.get("description"),
            "price_sgd": prod.get("price_sgd"),
            "interval": "annual" if "annual" in product_slug else "month",
            "status": s.status,
            "current_period_end": s.current_period_end.isoformat() if s.current_period_end else None,
            "started_at": s.created_at.isoformat() if s.created_at else None,
            "stripe_subscription_id": s.stripe_subscription_id,
            "stripe_customer_id": s.stripe_customer_id,
            # The product_type maps to a feature tier — surfacing per-sub
            # features lets the UI show "this is what you unlocked by buying
            # X" per tab instead of one merged blob.
            "features": (enforce_tier(
                assessment_data={
                    "plan": product_slug.replace("_monthly", "").replace("_annual", ""),
                    "subscription_status": "active",
                    "payment_confirmed": True,
                },
                framework=None,
            ).get("features") or {}),
        })

    return {
        "plan": plan_value,
        "plan_label": plan_label,
        "price_sgd": price_sgd,
        "tier": result.get("tier"),
        "subscription_started_at": (
            current_user.subscription_started_at.isoformat()
            if getattr(current_user, "subscription_started_at", None) else None
        ),
        "stripe_customer_id": getattr(current_user, "stripe_customer_id", None),
        "stripe_subscription_id": getattr(current_user, "stripe_subscription_id", None),
        "features": features,
        # New: every active Stripe subscription on this user, not just the
        # latest activation. Frontend tabs the list when len > 1.
        "all_subscriptions": all_subscriptions,
        "notarization": {
            "monthly_quota": quota,
            "used_this_month": used,
            "remaining": max(0, quota - used) if quota else 0,
        },
        "bundle_credits": {
            "notarization": int(getattr(current_user, "notarization_credits", 0) or 0),
            "compliance_evidence": int(getattr(current_user, "compliance_evidence_credits", 0) or 0),
        },
    }


@router.post("/subscription/portal")
async def vendor_subscription_portal(
    current_user=Depends(get_current_user),
):
    """Create a Stripe Billing Portal session so the vendor can manage their subscription."""
    import os as _os
    import stripe as _stripe

    cust_id = getattr(current_user, "stripe_customer_id", None)
    if not cust_id:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer on file. Complete a purchase first, or wait for the webhook to reconcile.",
        )

    secret = _os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe not configured.")
    _stripe.api_key = secret

    return_url = (
        _os.environ.get("NEXT_PUBLIC_BASE_URL")
        or _os.environ.get("BACKEND_BASE_URL")
        or "https://booppa.io"
    ).rstrip("/") + "/vendor/subscription"

    session = _stripe.billing_portal.Session.create(
        customer=cust_id,
        return_url=return_url,
    )
    return {"url": session.url}
