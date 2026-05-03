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


class GrantCreditsBody(BaseModel):
    email: str = Field(..., description="Customer email")
    credits: int = Field(..., ge=1, le=50, description="Notarization credits to add")
    pending_cover_sheet: bool = Field(False, description="Set to True for Compliance Evidence Pack backfill")


@router.post("/grant-credits")
def grant_credits(
    body: GrantCreditsBody,
    _auth: bool = Depends(_admin_auth),
):
    """Backfill notarization credits for a user (e.g. stuck bundle purchase before fan-out fix)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == body.email).first()
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
