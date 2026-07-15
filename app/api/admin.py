from app.core.route_classes import RetryAPIRoute
from fastapi import (
    APIRouter, Request, HTTPException, Query, Depends, UploadFile,
    File as FastAPIFile,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.db import SessionLocal
from app.core.repositories.user_repository import UserRepository
from app.core.models import ConsentLog, EnterpriseProfile, ActivityLog, VendorScore, User
from app.core.config import settings
from app.core.auth import create_admin_token, verify_admin_token
import logging
import secrets

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RetryAPIRoute)
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
    from app.core.repositories.consent_log_repository import ConsentLogRepository

    db = SessionLocal()
    try:
        rows = ConsentLogRepository.get_recent_logs(db, limit)
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
        from app.core.repositories.enterprise_profile_repository import EnterpriseProfileRepository
        active_windows = EnterpriseProfileRepository.count_active_procurement(db)
        
        # Calculate global pulse score (average of all active enterprise intent scores)
        profiles = EnterpriseProfileRepository.get_all_intent_scores(db)
        
        global_pulse = 0.0
        if profiles:
            global_pulse = sum((p.procurement_intent_score or 0) for p in profiles) / len(profiles)

        # Get top enterprises by intent score
        top_profiles = EnterpriseProfileRepository.get_top_profiles(db, limit=5)

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
    from app.core.models import TenderShortlist
    from app.core.repositories.tender_shortlist_repository import TenderShortlistRepository
    db = SessionLocal()
    try:
        existing = TenderShortlistRepository.get_by_tender_no(db, body.tender_no)
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
    from app.core.models import TenderShortlist
    from app.core.repositories.tender_shortlist_repository import TenderShortlistRepository
    db = SessionLocal()
    inserted = 0
    updated  = 0
    try:
        for item in body:
            existing = TenderShortlistRepository.get_by_tender_no(db, item.tender_no)
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
    from app.core.repositories.tender_shortlist_repository import TenderShortlistRepository
    db = SessionLocal()
    try:
        total, rows = TenderShortlistRepository.list_entries(db, sector, agency, offset, limit)
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
    from app.core.models import TenderShortlist
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(tender_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        from app.core.repositories.tender_shortlist_repository import TenderShortlistRepository
        row = TenderShortlistRepository.get_by_id(db, uid)
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
    from app.core.models import TenderShortlist
    import uuid as _uuid
    db = SessionLocal()
    try:
        try:
            uid = _uuid.UUID(tender_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid UUID")
        from app.core.repositories.tender_shortlist_repository import TenderShortlistRepository
        row = TenderShortlistRepository.get_by_id(db, uid)
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
        from app.core.models import MarketplaceVendor as Model
    else:
        from app.core.models import DiscoveredVendor as Model

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
    db = SessionLocal()
    try:
        total, rows = UserRepository.search_users(
            db,
            q=q,
            role=role,
            plan=plan,
            is_active=is_active,
            offset=offset,
            limit=limit,
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
        from app.core.repositories.report_repository import ReportRepository
        report = ReportRepository.get_by_id(db, str(body.report_id))
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
        user = UserRepository.get_by_email(db, body.email, lock_for_update=True)
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
    from app.core.models import MarketplaceVendor
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
    from app.core.models import MarketplaceVendor
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
    from app.core.models import MarketplaceVendor
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
    from app.core.models import MarketplaceVendor
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

# Canned brief used so admin test checkouts skip the /rfp-intake step entirely
# and fulfill the RFP kit immediately. Real user purchases still go through the
# brief intake — this default only applies inside simulate_purchase.
DEFAULT_QA_RFP_BRIEF = (
    "QA test brief: procurement for a SaaS vendor handling personal data. "
    "Evaluate PDPA compliance, data security controls (encryption, access "
    "management), sub-processor disclosure, and incident response readiness. "
    "Budget and timeline are illustrative — this is an internal Booppa QA run."
)


# Bundle SKUs intentionally removed from the admin test-checkout. They still work
# for real purchases; the test tool just no longer exercises them.
_TEST_CHECKOUT_DENYLIST = {"rfp_accelerator", "enterprise_bid_kit"}


class SimulatePurchaseRequest(BaseModel):
    product_type: str = Field(..., description="A product_type from MODE_MAP")
    customer_email: str = Field(..., description="Test email — receives real fulfillment mail")
    vendor_url: Optional[str] = Field(default="https://booppa.io")
    company_name: Optional[str] = Field(default="Booppa QA")
    uen: Optional[str] = Field(
        default=None,
        description="Optional Singapore UEN — exercises the offline "
        "DiscoveredVendor registry match + live ACRA lookup on the certificate.",
    )
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
    from app.services.fulfillment import (
        SUBSCRIPTION_PRODUCT_TYPES,
        BUNDLE_COMPONENTS,
        RFP_PRODUCT_TYPES,
        NOTARIZATION_PRODUCT_TYPES,
        PDPA_PRODUCT_TYPES,
        VENDOR_PROOF_PRODUCT_TYPES,
        CSP_ONETIME_PRODUCT_TYPES,
        activate_subscription,
        fulfill_bundle,
        fulfill_standalone_no_report,
        defer_rfp_to_intake,
    )
    from app.core.models import Report, Subscription
    from app.core.models import PendingRfpIntake

    product_type = body.product_type
    # `pdpa_snapshot` is a valid PDPA_PRODUCT_TYPES alias but is not a MODE_MAP key
    # (only the canonical `pdpa_quick_scan` SKU is priced). Normalise so a test
    # checkout submitting the alias isn't rejected 422.
    if product_type == "pdpa_snapshot":
        product_type = "pdpa_quick_scan"
    if product_type not in MODE_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown product_type — must be one of {sorted(MODE_MAP.keys())}",
        )
    # These bundle SKUs are retired from the test-checkout (they remain live for
    # real purchases). Reject so the tool stays consistent with the catalog UI.
    if product_type in _TEST_CHECKOUT_DENYLIST:
        raise HTTPException(
            status_code=422,
            detail=f"{product_type} is disabled in test checkout.",
        )

    customer_email = body.customer_email.strip().lower()
    vendor_url = (body.vendor_url or "").strip()
    company_name = (body.company_name or "").strip()
    uen = (body.uen or "").strip()
    rfp_description = (body.rfp_description or "").strip() or DEFAULT_QA_RFP_BRIEF
    sim_id = f"admin-sim-{_uuid.uuid4()}"

    # Ensure a User row exists for the test email so fulfillment helpers can attach
    # owner_id / grant credits / activate plans.
    db = SessionLocal()
    try:
        user = UserRepository.get_by_email(db, customer_email)
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
    if uen:
        # Threaded into the stub Report's assessment_data["uen"] so the
        # Vendor Proof fulfillment exercises the offline DiscoveredVendor
        # registry match + live ACRA status, exactly like a real purchase.
        metadata["uen"] = uen
    if rfp_description:
        metadata["rfp_description"] = rfp_description

    # Dispatch ---------------------------------------------------------------
    dispatch: str
    details: dict = {}

    if product_type in SUBSCRIPTION_PRODUCT_TYPES:
        dispatch = "subscription"
        # For buyer SKUs, fire the full [DEMO] deliverable fan-out (all 6 buyer
        # emails, mock tx hash, no gas) instead of just the first-cycle digest,
        # so the test checkout shows the complete buyer email set. `demo` only
        # affects the buyer branch in _activate_subscription and never touches
        # the live-webhook path (where demo derives from Stripe livemode).
        is_buyer = product_type.startswith("buyer_")
        await activate_subscription(
            product_type=product_type,
            customer_email=customer_email,
            stripe_subscription_id=sim_id,
            stripe_customer_id=sim_id,
            test_simulation=True,
            demo=is_buyer,
            # Test Identity drives first-cycle deliverables (Vendor snapshot,
            # PDPA Monitor report) without mutating the real user profile.
            override_company=company_name or None,
            override_website=vendor_url or None,
        )
        db = SessionLocal()
        try:
            u = UserRepository.get_by_email(db, customer_email)
            from app.core.repositories.subscription_repository import SubscriptionRepository
            sub = SubscriptionRepository.get_by_stripe_subscription_id(db, sim_id)
            details = {
                "plan": getattr(u, "plan", None),
                "subscription_id": str(sub.id) if sub else None,
                "stripe_subscription_id": sim_id,
            }
        finally:
            db.close()

    elif product_type in BUNDLE_COMPONENTS:
        dispatch = "bundle"
        await fulfill_bundle(
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

            from app.core.repositories.report_repository import ReportRepository
            stubs = ReportRepository.get_by_stripe_session_id(db, sim_id)
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
            # Compliance Evidence Pack now produces a BCEP EvidencePack (not stubs/
            # cover sheet) — surface its id so the test-checkout can link to the
            # generated pack at /evidence-pack-intake/{id}.
            if product_type == "compliance_evidence_pack":
                from app.core.models import EvidencePack

                ep = (
                    db.query(EvidencePack)
                    .filter(EvidencePack.session_id == sim_id)
                    .order_by(EvidencePack.created_at.desc())
                    .first()
                )
                if ep:
                    details["evidence_pack_id"] = str(ep.id)
                    details["pack_id"] = ep.pack_id
        finally:
            db.close()

    elif product_type in RFP_PRODUCT_TYPES:
        if rfp_description:
            dispatch = "rfp"
            from app.workers.tasks import fulfill_rfp_task

            db = SessionLocal()
            try:
                u = UserRepository.get_by_email(db, customer_email)
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
                # Admin test-checkout only: ship the kit with any residual
                # [Verify: …] placeholders instead of blocking on the canned
                # brief and routing to /rfp-intake. Mirrors the bundle test path
                # (stripe_webhook.py). Real purchases never set this.
                allow_incomplete=True,
            )
            details = {"session_id": sim_id, "queued": "fulfill_rfp_task"}
        else:
            dispatch = "rfp-deferred"
            intake_id = await defer_rfp_to_intake(
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
        | CSP_ONETIME_PRODUCT_TYPES
    ):
        dispatch = "standalone"
        await fulfill_standalone_no_report(
            product_type=product_type,
            customer_email=customer_email,
            metadata=metadata,
            session_id=sim_id,
        )
        db = SessionLocal()
        try:
            from sqlalchemy import String, cast

            from app.core.repositories.report_repository import ReportRepository
            stub_list = ReportRepository.get_by_stripe_session_id(db, sim_id)
            stub = stub_list[0] if stub_list else None
            u = UserRepository.get_by_email(db, customer_email)
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



# ── PDPA bulk scan (CSV/XLSX of companies → rate-limited free scans) ──────────
# Testing/prospecting tool: upload up to MAX_BULK_SCAN_ROWS rows of
# (company_name, website_url); each becomes a bulk_pdpa_scan_item_task on the
# `reports` queue, throttled to 20/min so a 600-row batch drains in ~30 minutes
# without starving paid fulfillment work.

MAX_BULK_SCAN_ROWS = 1000
_BULK_SCAN_COLUMNS = {"company_name", "website_url"}


def _parse_bulk_scan_rows(filename: str, content: bytes) -> list[dict]:
    """Return [{company_name, website_url}, …] from CSV or XLSX bytes.

    Header match is case-insensitive and tolerates extra columns. Rows without
    a website URL are dropped; URLs are deduped (first occurrence wins).
    """
    import csv
    import io

    name = (filename or "").lower()
    raw_rows: list[dict] = []

    if name.endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed on server")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            rows_data = list(ws.iter_rows(values_only=True))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse Excel file: {exc}")
        if not rows_data:
            raise HTTPException(status_code=400, detail="Excel file is empty")
        headers = [str(h).strip().lower() if h else "" for h in rows_data[0]]
        for vals in rows_data[1:]:
            raw_rows.append({
                headers[i]: (str(v).strip() if v is not None else "")
                for i, v in enumerate(vals) if i < len(headers)
            })
    else:
        try:
            text = content.decode("utf-8-sig")  # utf-8-sig handles BOM from Excel exports
            reader = csv.DictReader(io.StringIO(text))
            headers = [h.strip().lower() for h in (reader.fieldnames or [])]
            for row in reader:
                raw_rows.append({
                    (k or "").strip().lower(): (v or "").strip() for k, v in row.items()
                })
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse CSV file: {exc}")

    missing = _BULK_SCAN_COLUMNS - set(headers)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"File missing required columns: {', '.join(sorted(missing))}. "
                   f"Expected headers: company_name, website_url",
        )

    seen_urls: set[str] = set()
    rows: list[dict] = []
    for row in raw_rows:
        url = (row.get("website_url") or "").strip()
        company = (row.get("company_name") or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        key = url.lower().rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        rows.append({"company_name": company or url, "website_url": url[:500]})
        if len(rows) > MAX_BULK_SCAN_ROWS:
            raise HTTPException(
                status_code=400,
                detail=f"File exceeds maximum {MAX_BULK_SCAN_ROWS} rows. Split into multiple files.",
            )
    if not rows:
        raise HTTPException(status_code=400, detail="No rows with a website_url found in file")
    return rows


@router.post("/pdpa/bulk-scan")
async def create_pdpa_bulk_scan(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    _auth: bool = Depends(_admin_auth),
) -> dict:
    from app.core.models import PdpaBulkScanBatch, PdpaBulkScanItem
    from app.workers.tasks import bulk_pdpa_scan_item_task

    content = await file.read()
    rows = _parse_bulk_scan_rows(file.filename or "", content)

    db = SessionLocal()
    try:
        batch = PdpaBulkScanBatch(filename=(file.filename or "")[:255], total=len(rows))
        db.add(batch)
        db.flush()
        items = [
            PdpaBulkScanItem(batch_id=batch.id, company_name=r["company_name"][:255],
                             website_url=r["website_url"])
            for r in rows
        ]
        db.add_all(items)
        db.commit()
        batch_id = str(batch.id)
        item_ids = [str(i.id) for i in items]
    finally:
        db.close()

    # Stagger enqueue as a second safety layer on top of the task's
    # rate_limit="20/m", so a worker restart can't burst-drain the backlog.
    for idx, item_id in enumerate(item_ids):
        bulk_pdpa_scan_item_task.apply_async(args=[item_id], countdown=idx * 3)

    logger.info(f"[pdpa-bulk-scan] batch={batch_id} queued {len(item_ids)} scans")
    return {"ok": True, "batch_id": batch_id, "total": len(item_ids)}


@router.get("/pdpa/bulk-scan/{batch_id}")
def get_pdpa_bulk_scan(
    batch_id: str,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _auth: bool = Depends(_admin_auth),
) -> dict:
    from sqlalchemy import func
    from app.core.models import PdpaBulkScanBatch, PdpaBulkScanItem

    db = SessionLocal()
    try:
        batch = db.query(PdpaBulkScanBatch).filter(PdpaBulkScanBatch.id == batch_id).first()
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        counts = dict(
            db.query(PdpaBulkScanItem.status, func.count())
            .filter(PdpaBulkScanItem.batch_id == batch.id)
            .group_by(PdpaBulkScanItem.status)
            .all()
        )
        items = (
            db.query(PdpaBulkScanItem)
            .filter(PdpaBulkScanItem.batch_id == batch.id)
            .order_by(PdpaBulkScanItem.created_at, PdpaBulkScanItem.id)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "batch_id": str(batch.id),
            "filename": batch.filename,
            "total": batch.total,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "counts": {
                "pending": counts.get("pending", 0),
                "running": counts.get("running", 0),
                "done": counts.get("done", 0),
                "failed": counts.get("failed", 0),
            },
            "items": [
                {
                    "id": str(i.id),
                    "company_name": i.company_name,
                    "website_url": i.website_url,
                    "status": i.status,
                    "score": (i.result or {}).get("score"),
                    "risk_level": (i.result or {}).get("risk_level"),
                    "total_findings": (i.result or {}).get("total_findings"),
                    "error": i.error,
                    "finished_at": i.finished_at.isoformat() if i.finished_at else None,
                }
                for i in items
            ],
        }
    finally:
        db.close()


@router.get("/pdpa/bulk-scan/{batch_id}/export")
def export_pdpa_bulk_scan(
    batch_id: str,
    _auth: bool = Depends(_admin_auth),
):
    import csv
    import io
    from fastapi.responses import StreamingResponse
    from app.core.models import PdpaBulkScanBatch, PdpaBulkScanItem

    db = SessionLocal()
    try:
        batch = db.query(PdpaBulkScanBatch).filter(PdpaBulkScanBatch.id == batch_id).first()
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        items = (
            db.query(PdpaBulkScanItem)
            .filter(PdpaBulkScanItem.batch_id == batch.id)
            .order_by(PdpaBulkScanItem.created_at, PdpaBulkScanItem.id)
            .all()
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "company_name", "website_url", "status", "score", "risk_level",
            "total_findings", "top_finding", "error",
        ])
        for i in items:
            result = i.result or {}
            top = (result.get("free_finding") or {}).get("title", "")
            writer.writerow([
                i.company_name, i.website_url, i.status, result.get("score", ""),
                result.get("risk_level", ""), result.get("total_findings", ""),
                top, i.error or "",
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="pdpa_bulk_scan_{batch_id}.csv"'
            },
        )
    finally:
        db.close()
