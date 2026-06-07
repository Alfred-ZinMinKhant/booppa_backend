from fastapi import APIRouter, Request, HTTPException, Query, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.db import SessionLocal
from app.core.models import ConsentLog, EnterpriseProfile, ActivityLog, VendorScore, User
from app.core.config import settings
from app.core.auth import create_admin_token, verify_admin_token
import logging
import secrets

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)


def _admin_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
):
    """Accept (in order): Bearer admin JWT, X-Admin-Token header, or HTTP Basic creds."""
    # 1. Bearer admin JWT (used by the new /admin/login flow)
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        payload = verify_admin_token(token)
        if payload and payload.get("sub"):
            return True

    # 2. Static X-Admin-Token header (legacy machine-to-machine)
    header = request.headers.get("x-admin-token")
    if settings.ADMIN_TOKEN:
        if header and secrets.compare_digest(header, settings.ADMIN_TOKEN):
            return True

    # 3. HTTP Basic (for direct API hits / curl)
    if settings.ADMIN_USER and settings.ADMIN_PASSWORD and credentials:
        valid_user = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
        valid_pass = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
        if valid_user and valid_pass:
            return True

    logger.warning("Admin authentication failed")
    raise HTTPException(status_code=401, detail="Unauthorized")


# ── Admin login / logout ──────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


@router.post("/login", response_model=AdminLoginResponse)
def admin_login(body: AdminLoginRequest):
    if not (settings.ADMIN_USER and settings.ADMIN_PASSWORD):
        raise HTTPException(status_code=503, detail="Admin login is not configured.")
    valid_user = secrets.compare_digest(body.username, settings.ADMIN_USER)
    valid_pass = secrets.compare_digest(body.password, settings.ADMIN_PASSWORD)
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")
    return AdminLoginResponse(
        access_token=create_admin_token(body.username),
        username=body.username,
    )


@router.post("/logout")
def admin_logout():
    """Stateless JWT — client just deletes the cookie. Endpoint exists for symmetry."""
    return {"ok": True}


@router.get("/me")
def admin_me(_auth: bool = Depends(_admin_auth)):
    return {"ok": True}


@router.get("/consent/logs")
def list_consent_logs(
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    _auth: bool = Depends(_admin_auth),
) -> List[dict]:
    """Return recent consent logs for quick verification. Protected by admin auth."""

    db = SessionLocal()
    try:
        rows = (
            db.query(ConsentLog)
            .order_by(ConsentLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        results = []
        for r in rows:
            results.append(
                {
                    "id": str(r.id),
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "ip_anonymized": r.ip_anonymized,
                    "consent_status": r.consent_status,
                    "policy_version": r.policy_version,
                    "metadata": r.metadata_json,
                }
            )
        return results
    finally:
        db.close()

@router.get("/intelligence")
def get_ecosystem_intelligence(
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Return aggregated ecosystem intelligence data for the Admin Dashboard."""
    db = SessionLocal()
    try:
        # Calculate real metrics from the database
        active_windows = db.query(EnterpriseProfile).filter(EnterpriseProfile.active_procurement == True).count()
        
        # Calculate global pulse score (average of all active enterprise intent scores)
        profiles = db.query(EnterpriseProfile).filter(
            EnterpriseProfile.procurement_intent_score.isnot(None)
        ).all()
        
        global_pulse = 0.0
        if profiles:
            global_pulse = sum((p.procurement_intent_score or 0) for p in profiles) / len(profiles)

        # Get top enterprises by intent score
        top_profiles = db.query(EnterpriseProfile).filter(
            EnterpriseProfile.procurement_intent_score.isnot(None)
        ).order_by(
            EnterpriseProfile.procurement_intent_score.desc()
        ).limit(5).all()

        top_enterprises = []
        for p in top_profiles:
            top_enterprises.append({
                "domain": p.domain,
                "score": p.procurement_intent_score,
                "industry": p.organization_type.value if hasattr(p, 'organization_type') and p.organization_type else "Enterprise",
                "value": "High Intent",
                "status": "Triggered" if p.active_procurement else "Monitoring"
            })

        # Historical index data — populated by timeseries pipeline (empty until data exists)
        index_data: list = []

        return {
            "globalPulse": round(float(global_pulse), 1),
            "activeWindows": active_windows,
            "vulnerableVectors": 0,
            "enterpriseValue": len(profiles) * 50000,
            "indexData": index_data,
            "topEnterprises": top_enterprises
        }
    finally:
        db.close()


# ── TenderShortlist CRUD ──────────────────────────────────────────────────────

class TenderIn(BaseModel):
    tender_no:   str
    sector:      str
    agency:      str
    description: Optional[str] = None
    base_rate:   float = Field(default=0.20, ge=0.0, le=1.0)


@router.post("/tenders", status_code=201)
def create_tender(
    body: TenderIn,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Create a single TenderShortlist entry."""
    from app.core.models_v10 import TenderShortlist
    db = SessionLocal()
    try:
        existing = db.query(TenderShortlist).filter(
            TenderShortlist.tender_no == body.tender_no
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="tender_no already exists")
        row = TenderShortlist(**body.model_dump())
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": str(row.id), "tender_no": row.tender_no}
    finally:
        db.close()


@router.post("/tenders/bulk", status_code=201)
def bulk_create_tenders(
    body: List[TenderIn],
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Upsert a list of tender entries (insert or update by tender_no)."""
    from app.core.models_v10 import TenderShortlist
    db = SessionLocal()
    inserted = 0
    updated  = 0
    try:
        for item in body:
            existing = db.query(TenderShortlist).filter(
                TenderShortlist.tender_no == item.tender_no
            ).first()
            if existing:
                for k, v in item.model_dump().items():
                    setattr(existing, k, v)
                updated += 1
            else:
                db.add(TenderShortlist(**item.model_dump()))
                inserted += 1
        db.commit()
        return {"inserted": inserted, "updated": updated, "total": len(body)}
    finally:
        db.close()


@router.get("/tenders")
def list_tenders(
    sector: Optional[str] = Query(None),
    agency: Optional[str] = Query(None),
    limit:  int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """List TenderShortlist entries with optional sector/agency filters."""
    from app.core.models_v10 import TenderShortlist
    db = SessionLocal()
    try:
        q = db.query(TenderShortlist)
        if sector:
            q = q.filter(TenderShortlist.sector == sector)
        if agency:
            q = q.filter(TenderShortlist.agency == agency)
        total = q.count()
        rows  = q.order_by(TenderShortlist.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total":  total,
            "items": [
                {
                    "id":          str(r.id),
                    "tender_no":   r.tender_no,
                    "sector":      r.sector,
                    "agency":      r.agency,
                    "description": r.description,
                    "base_rate":   r.base_rate,
                    "created_at":  r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.put("/tenders/{tender_id}")
def update_tender(
    tender_id: str,
    body: TenderIn,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Update a single TenderShortlist entry by UUID."""
    from app.core.models_v10 import TenderShortlist
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(tender_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        row = db.query(TenderShortlist).filter(TenderShortlist.id == uid).first()
        if not row:
            raise HTTPException(status_code=404, detail="Tender not found")
        
        # Update fields
        for k, v in body.model_dump().items():
            setattr(row, k, v)
            
        db.commit()
        db.refresh(row)
        return {"id": str(row.id), "tender_no": row.tender_no, "status": "updated"}
    finally:
        db.close()


@router.post("/tenders/refresh-base-rates", status_code=202)
def trigger_gebiz_base_rate_refresh(
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """
    Trigger an immediate GeBIZ base_rate refresh from data.gov.sg.
    Enqueues the Celery task and returns immediately.
    """
    from app.workers.tasks import refresh_gebiz_base_rates
    task = refresh_gebiz_base_rates.delay()
    return {"queued": True, "task_id": task.id}


@router.post("/tenders/sync-gebiz", status_code=202)
def trigger_gebiz_sync(
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """
    Trigger an immediate GeBIZ live tender sync (RSS + scrape).
    Enqueues the Celery task and returns immediately.
    """
    from app.workers.tasks import sync_gebiz_tenders
    task = sync_gebiz_tenders.delay()
    return {"queued": True, "task_id": task.id}


@router.post("/tenders/send-intelligence-digest", status_code=202)
def trigger_tender_intelligence_digest(
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """
    Trigger an immediate Tender Intelligence monthly digest send.
    Useful for QA before the 1st-of-month schedule fires.
    """
    from app.workers.tasks import send_tender_intelligence_digest
    task = send_tender_intelligence_digest.delay()
    return {"queued": True, "task_id": task.id}


@router.delete("/tenders/{tender_id}", status_code=204)
def delete_tender(
    tender_id: str,
    _auth: bool = Depends(_admin_auth),
):
    """Delete a TenderShortlist entry by UUID."""
    from app.core.models_v10 import TenderShortlist
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(tender_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        row = db.query(TenderShortlist).filter(TenderShortlist.id == uid).first()
        if not row:
            raise HTTPException(status_code=404, detail="Tender not found")
        db.delete(row)
        db.commit()
    finally:
        db.close()


# ── Vendor Contact Scraping ──────────────────────────────────────────────────


@router.post("/scrape-vendors", status_code=202)
def trigger_vendor_scrape(
    model: str = Query("marketplace", regex="^(marketplace|discovered)$"),
    limit: int = Query(50, ge=1, le=500),
    _auth: bool = Depends(_admin_auth),
):
    """Queue batch scraping for vendors missing contact emails."""
    from app.workers.tasks import scrape_vendor_contacts_batch
    scrape_vendor_contacts_batch.delay(model=model, limit=limit)
    return {"status": "queued", "model": model, "limit": limit}


@router.post("/scrape-vendor/{vendor_id}", status_code=202)
def trigger_single_vendor_scrape(
    vendor_id: str,
    model: str = Query("marketplace", regex="^(marketplace|discovered)$"),
    _auth: bool = Depends(_admin_auth),
):
    """Queue scraping for a single vendor by ID."""
    from app.workers.tasks import scrape_vendor_contact_task
    scrape_vendor_contact_task.delay(vendor_id, model=model)
    return {"status": "queued", "vendor_id": vendor_id, "model": model}


@router.get("/scrape-stats")
def scrape_stats(
    model: str = Query("marketplace", regex="^(marketplace|discovered)$"),
    _auth: bool = Depends(_admin_auth),
):
    """Get scraping coverage stats."""
    from sqlalchemy import func
    if model == "marketplace":
        from app.core.models_v10 import MarketplaceVendor as Model
    else:
        from app.core.models_v10 import DiscoveredVendor as Model

    db = SessionLocal()
    try:
        total = db.query(func.count(Model.id)).scalar()
        with_email = db.query(func.count(Model.id)).filter(Model.contact_email.isnot(None)).scalar()
        with_website = db.query(func.count(Model.id)).filter(Model.website.isnot(None)).scalar()
        scraped = db.query(func.count(Model.id)).filter(Model.last_scraped_at.isnot(None)).scalar()
        return {
            "model": model,
            "total": total,
            "with_email": with_email,
            "with_website": with_website,
            "scraped": scraped,
            "coverage_pct": round(with_email / total * 100, 1) if total else 0,
        }
    finally:
        db.close()


# ── User directory ────────────────────────────────────────────────────────────


@router.get("/users")
def admin_list_users(
    q: Optional[str] = Query(None, description="Search by email, full name, or company"),
    role: Optional[str] = Query(None, description="Filter by role (e.g. VENDOR, PROCUREMENT, ADMIN)"),
    plan: Optional[str] = Query(None, description="Filter by plan slug"),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Paginated user directory for the admin console."""
    from sqlalchemy import or_

    db = SessionLocal()
    try:
        query = db.query(User)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(
                or_(
                    User.email.ilike(like),
                    User.full_name.ilike(like),
                    User.company.ilike(like),
                )
            )
        if role:
            query = query.filter(User.role == role)
        if plan:
            query = query.filter(User.plan == plan)
        if is_active is not None:
            query = query.filter(User.is_active == is_active)

        total = query.count()
        rows = (
            query.order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = []
        for u in rows:
            items.append({
                "id": str(u.id),
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "company": u.company,
                "uen": u.uen,
                "plan": u.plan,
                "subscription_tier": u.subscription_tier,
                "is_active": bool(u.is_active),
                "verified": bool(u.verified_at),
                "has_stripe_subscription": bool(u.stripe_subscription_id),
                "notarization_credits": int(u.notarization_credits or 0),
                "compliance_evidence_credits": int(u.compliance_evidence_credits or 0),
                "signed_cover_sheet_uploaded": bool(u.signed_cover_sheet_uploaded),
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
        return {"total": total, "items": items}
    finally:
        db.close()


class GrantCreditsBody(BaseModel):
    email: str = Field(..., description="Customer email")
    credits: int = Field(..., ge=1, le=50, description="Notarization credits to add")
    pending_cover_sheet: bool = Field(False, description="Set to True for Compliance Evidence Pack backfill")


class RetryAnchorBody(BaseModel):
    report_id: str = Field(..., description="UUID of the Report to re-anchor")


@router.post("/retry-anchor")
def retry_anchor(
    body: RetryAnchorBody,
    _auth: bool = Depends(_admin_auth),
):
    """
    Re-queue ``anchor_signed_cover_sheet_task`` for a specific Report.

    Use when a signed Cover Sheet's on-chain anchor failed (e.g. the original
    worker hit the now-fixed "already-anchored → None tx_hash" bug, or RPC
    timed out, or the contract reverted). Clears any persisted anchor_failed
    flag so the frontend stops showing the failure card while the retry runs.
    """
    from app.core.models import Report
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == body.report_id).first()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        if report.framework != "compliance_evidence_signed_sheet":
            raise HTTPException(
                status_code=400,
                detail=f"Report framework is {report.framework!r}; retry-anchor only handles signed-CS reports.",
            )
        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        # Clear failure flag so UI stops showing the red card while the
        # retry is in flight. The worker will set it back if it fails again.
        ad.pop("anchor_failed", None)
        ad.pop("anchor_failed_at", None)
        ad.pop("anchor_failed_reason", None)
        # Also clear any prior partial tx_hash so the UI shows the spinner
        # for the duration of the retry instead of pointing at a stale tx.
        report.assessment_data = ad
        customer_email = ad.get("contact_email")
        company_name = report.company_name or ""
        db.commit()
        logger.info(
            f"[retry-anchor] Cleared anchor_failed for report={body.report_id}, "
            f"requeuing worker"
        )
    finally:
        db.close()

    from app.workers.tasks import anchor_signed_cover_sheet_task
    anchor_signed_cover_sheet_task.apply_async(
        kwargs={
            "report_id": body.report_id,
            "customer_email": customer_email,
            "company_name": company_name,
        },
        countdown=2,
    )
    return {
        "ok": True,
        "report_id": body.report_id,
        "queued": True,
        "note": "Anchor retry queued. The Cover Sheet page will refresh within 30-60s.",
    }


@router.post("/grant-credits")
def grant_credits(
    body: GrantCreditsBody,
    _auth: bool = Depends(_admin_auth),
):
    """Backfill notarization credits for a user (e.g. stuck bundle purchase before fan-out fix)."""
    db = SessionLocal()
    try:
        # Row-locked so two concurrent admin grants for the same email apply
        # additively instead of lost-update overwriting each other.
        user = (
            db.query(User)
            .filter(User.email == body.email)
            .with_for_update()
            .first()
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"No user with email {body.email}")
        current = getattr(user, "notarization_credits", 0) or 0
        user.notarization_credits = current + body.credits
        if body.pending_cover_sheet:
            user.pending_cover_sheet = True
        db.commit()
        db.refresh(user)
        logger.info(f"Granted {body.credits} credits to {body.email} (new balance: {user.notarization_credits})")
        return {
            "email": user.email,
            "credits_granted": body.credits,
            "new_balance": user.notarization_credits,
            "pending_cover_sheet": bool(getattr(user, "pending_cover_sheet", False)),
        }
    finally:
        db.close()


# ── Marketplace Vendor CRUD ───────────────────────────────────────────────────

class VendorIn(BaseModel):
    company_name: str
    slug: Optional[str] = None
    domain: Optional[str] = None
    website: Optional[str] = None
    uen: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = "Singapore"
    city: Optional[str] = None
    short_description: Optional[str] = None
    contact_email: Optional[str] = None


def _serialize_vendor(v) -> dict:
    return {
        "id": str(v.id),
        "company_name": v.company_name,
        "slug": v.slug,
        "domain": v.domain,
        "website": v.website,
        "uen": v.uen,
        "industry": v.industry,
        "country": v.country,
        "city": v.city,
        "short_description": v.short_description,
        "contact_email": v.contact_email,
        "scan_status": v.scan_status,
        "claimed_by_user_id": str(v.claimed_by_user_id) if v.claimed_by_user_id else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


@router.get("/vendors")
def admin_list_vendors(
    q: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _auth: bool = Depends(_admin_auth),
) -> dict:
    from app.core.models_v10 import MarketplaceVendor
    db = SessionLocal()
    try:
        query = db.query(MarketplaceVendor)
        if q:
            like = f"%{q}%"
            query = query.filter(MarketplaceVendor.company_name.ilike(like))
        if industry:
            query = query.filter(MarketplaceVendor.industry == industry)
        total = query.count()
        rows = query.order_by(MarketplaceVendor.created_at.desc()).offset(offset).limit(limit).all()
        return {"total": total, "items": [_serialize_vendor(r) for r in rows]}
    finally:
        db.close()


@router.post("/vendors", status_code=201)
def admin_create_vendor(body: VendorIn, _auth: bool = Depends(_admin_auth)) -> dict:
    from app.core.models_v10 import MarketplaceVendor
    db = SessionLocal()
    try:
        slug = body.slug
        if not slug:
            import re
            slug = re.sub(r"[^a-z0-9]+", "-", body.company_name.lower()).strip("-")[:240]
        existing = db.query(MarketplaceVendor).filter(MarketplaceVendor.slug == slug).first()
        if existing:
            raise HTTPException(status_code=409, detail="Slug already exists.")
        v = MarketplaceVendor(
            company_name=body.company_name, slug=slug,
            domain=body.domain, website=body.website, uen=body.uen,
            industry=body.industry, country=body.country or "Singapore",
            city=body.city, short_description=body.short_description,
            contact_email=body.contact_email, source="manual",
        )
        db.add(v)
        db.commit()
        db.refresh(v)
        return _serialize_vendor(v)
    finally:
        db.close()


@router.put("/vendors/{vendor_id}")
def admin_update_vendor(vendor_id: str, body: VendorIn, _auth: bool = Depends(_admin_auth)) -> dict:
    from app.core.models_v10 import MarketplaceVendor
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(vendor_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        v = db.query(MarketplaceVendor).filter(MarketplaceVendor.id == uid).first()
        if not v:
            raise HTTPException(status_code=404, detail="Vendor not found")
        for k, val in body.model_dump(exclude_none=True).items():
            setattr(v, k, val)
        db.commit()
        db.refresh(v)
        return _serialize_vendor(v)
    finally:
        db.close()


@router.delete("/vendors/{vendor_id}", status_code=204)
def admin_delete_vendor(vendor_id: str, _auth: bool = Depends(_admin_auth)):
    from app.core.models_v10 import MarketplaceVendor
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(vendor_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        v = db.query(MarketplaceVendor).filter(MarketplaceVendor.id == uid).first()
        if not v:
            raise HTTPException(status_code=404, detail="Vendor not found")
        db.delete(v)
        db.commit()
    finally:
        db.close()


# ── Simulate purchase (test harness) ──────────────────────────────────────────
#
# Lets admin/QA exercise the full webhook fulfillment path for every paid SKU
# without going through Stripe. Calls the same helpers a real
# checkout.session.completed event would call — DB rows, Celery tasks, emails,
# and Amoy testnet anchors are produced for real. Test records are tagged so
# they can be identified later (Report.assessment_data.test_simulation = true,
# and stripe_*_id / session_id prefixed with "admin-sim-").


class SimulatePurchaseRequest(BaseModel):
    product_type: str = Field(..., description="A product_type from MODE_MAP")
    customer_email: str = Field(..., description="Test email — receives real fulfillment mail")
    vendor_url: Optional[str] = Field(default="https://booppa.io")
    company_name: Optional[str] = Field(default="Booppa QA")
    rfp_description: Optional[str] = Field(default=None)


@router.post("/simulate-purchase")
async def simulate_purchase(
    body: SimulatePurchaseRequest,
    _auth: bool = Depends(_admin_auth),
):
    """Simulate a Stripe checkout.session.completed event for any SKU."""
    import uuid as _uuid

    # Lazy imports so admin module can load even if Stripe wiring breaks at boot.
    from app.api.stripe_checkout import MODE_MAP
    from app.api.stripe_webhook import (
        SUBSCRIPTION_PRODUCT_TYPES,
        BUNDLE_COMPONENTS,
        RFP_PRODUCT_TYPES,
        NOTARIZATION_PRODUCT_TYPES,
        PDPA_PRODUCT_TYPES,
        VENDOR_PROOF_PRODUCT_TYPES,
        _activate_subscription,
        _fulfill_bundle,
        _fulfill_standalone_no_report,
        _defer_rfp_to_intake,
    )
    from app.core.models import Report, Subscription
    from app.core.models_v12 import PendingRfpIntake

    product_type = body.product_type
    if product_type not in MODE_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown product_type — must be one of {sorted(MODE_MAP.keys())}",
        )

    customer_email = body.customer_email.strip().lower()
    vendor_url = (body.vendor_url or "").strip()
    company_name = (body.company_name or "").strip()
    rfp_description = (body.rfp_description or "").strip()
    sim_id = f"admin-sim-{_uuid.uuid4()}"

    # Ensure a User row exists for the test email so fulfillment helpers can attach
    # owner_id / grant credits / activate plans.
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == customer_email).first()
        if not user:
            from app.core.auth import get_password_hash

            user = User(
                email=customer_email,
                hashed_password=get_password_hash(_uuid.uuid4().hex),
                full_name="Booppa QA",
                role="VENDOR",
                company=company_name or "Booppa QA",
                website=vendor_url or "https://booppa.io",
                is_active=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"[simulate-purchase] Created test user {customer_email}")
        else:
            # Backfill profile so handlers that read user.website / user.company succeed.
            updated = False
            if vendor_url and not user.website:
                user.website = vendor_url; updated = True
            if company_name and not user.company:
                user.company = company_name; updated = True
            if updated:
                db.commit()
    finally:
        db.close()

    metadata = {
        "company_name": company_name,
        "vendor_url": vendor_url,
        "customer_email": customer_email,
        "test_simulation": "1",
    }
    if rfp_description:
        metadata["rfp_description"] = rfp_description

    # Dispatch ---------------------------------------------------------------
    dispatch: str
    details: dict = {}

    if product_type in SUBSCRIPTION_PRODUCT_TYPES:
        dispatch = "subscription"
        await _activate_subscription(
            product_type=product_type,
            customer_email=customer_email,
            stripe_subscription_id=sim_id,
            stripe_customer_id=sim_id,
        )
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == customer_email).first()
            sub = (
                db.query(Subscription)
                .filter(Subscription.stripe_subscription_id == sim_id)
                .first()
            )
            details = {
                "plan": getattr(u, "plan", None),
                "subscription_id": str(sub.id) if sub else None,
                "stripe_subscription_id": sim_id,
            }
        finally:
            db.close()

    elif product_type in BUNDLE_COMPONENTS:
        dispatch = "bundle"
        await _fulfill_bundle(
            product_type=product_type,
            report_id=None,
            customer_email=customer_email,
            metadata=metadata,
            session_id=sim_id,
        )
        # Surface any rows the bundle just created.
        db = SessionLocal()
        try:
            from sqlalchemy import String, cast

            stubs = (
                db.query(Report)
                .filter(cast(Report.assessment_data["stripe_session_id"], String) == sim_id)
                .all()
            )
            pending = (
                db.query(PendingRfpIntake)
                .filter(PendingRfpIntake.session_id == sim_id)
                .first()
            )
            details = {
                "session_id": sim_id,
                "stub_report_ids": [str(s.id) for s in stubs],
                "pending_rfp_intake_id": str(pending.id) if pending else None,
            }
        finally:
            db.close()

    elif product_type in RFP_PRODUCT_TYPES:
        if rfp_description:
            dispatch = "rfp"
            from app.workers.tasks import fulfill_rfp_task

            db = SessionLocal()
            try:
                u = db.query(User).filter(User.email == customer_email).first()
                vendor_id = str(u.id) if u else customer_email
            finally:
                db.close()
            fulfill_rfp_task.delay(
                product_type=product_type,
                vendor_id=vendor_id,
                vendor_email=customer_email,
                vendor_url=vendor_url or "https://booppa.io",
                company_name=company_name or "Booppa QA",
                rfp_description=rfp_description,
                session_id=sim_id,
                intake_data=None,
            )
            details = {"session_id": sim_id, "queued": "fulfill_rfp_task"}
        else:
            dispatch = "rfp-deferred"
            intake_id = await _defer_rfp_to_intake(
                rfp_product_type=product_type,
                bundle_source=product_type,
                customer_email=customer_email,
                vendor_url=vendor_url or None,
                company_name=company_name or None,
                session_id=sim_id,
            )
            details = {"session_id": sim_id, "pending_rfp_intake_id": intake_id}

    elif product_type in (
        NOTARIZATION_PRODUCT_TYPES | PDPA_PRODUCT_TYPES | VENDOR_PROOF_PRODUCT_TYPES
    ):
        dispatch = "standalone"
        await _fulfill_standalone_no_report(
            product_type=product_type,
            customer_email=customer_email,
            metadata=metadata,
            session_id=sim_id,
        )
        db = SessionLocal()
        try:
            from sqlalchemy import String, cast

            stub = (
                db.query(Report)
                .filter(cast(Report.assessment_data["stripe_session_id"], String) == sim_id)
                .first()
            )
            u = db.query(User).filter(User.email == customer_email).first()
            details = {
                "session_id": sim_id,
                "report_id": str(stub.id) if stub else None,
                "notarization_credits": getattr(u, "notarization_credits", None),
            }
        finally:
            db.close()

    else:
        # MODE_MAP key not classified — should not happen if all 22 SKUs are covered.
        raise HTTPException(
            status_code=500,
            detail=f"product_type {product_type!r} is in MODE_MAP but no handler bucket matches",
        )

    logger.info(
        f"[simulate-purchase] product={product_type} dispatch={dispatch} "
        f"email={customer_email} sim_id={sim_id} details={details}"
    )
    return {
        "ok": True,
        "product_type": product_type,
        "dispatch": dispatch,
        "details": details,
    }

