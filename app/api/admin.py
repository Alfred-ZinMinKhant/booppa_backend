from app.core.route_classes import RetryAPIRoute
from fastapi import (
    APIRouter, Request, HTTPException, Query, Depends, UploadFile,
    File as FastAPIFile,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator
from app.core.validators import validate_name_field
from typing import List, Optional
from app.core.db import SessionLocal
from app.core.repositories.user_repository import UserRepository
from app.core.models import ConsentLog, EnterpriseProfile, ActivityLog, VendorScore, User
from app.core.config import settings
from app.core.auth import create_admin_token, verify_admin_token
import hashlib as _hashlib
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


class RequeueReportBody(BaseModel):
    report_id: str = Field(..., description="UUID of the Report to re-run through process_report_task")


class RefulfillBody(BaseModel):
    report_id: str = Field(..., description="UUID of the paid Report to re-run delivery for")


class RefundBody(BaseModel):
    """Either handle works: a report id (we look the session up) or the session id.

    Support tickets usually arrive with one or the other, rarely both.
    """

    report_id: Optional[str] = Field(None, description="UUID of the paid Report to refund")
    session_id: Optional[str] = Field(None, description="Stripe Checkout Session id")
    reason: str = Field(..., min_length=3, max_length=500, description="Why this is being refunded")


# Delivery evidence per framework: what proves the buyer actually RECEIVED the
# thing they paid for. `status == "completed"` is not that proof — it only means
# the scan finished. The stranded PDPA Quick Scan that prompted this was
# `completed` with no certificate, no PDF and no email.
_FULFILLMENT_FRAMEWORKS = ("pdpa_quick_scan", "vendor_proof", "compliance_notarization")


def _delivery_evidence(db, report) -> tuple[bool, str]:
    """Return (delivered, human-readable evidence) for a paid report."""
    from app.core.models import CertificateLog, VerifyRecord

    ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}

    if report.framework == "pdpa_quick_scan":
        cert = (
            db.query(CertificateLog)
            .filter(
                CertificateLog.report_id == report.id,
                CertificateLog.certificate_type == "PDPA",
            )
            .first()
        )
        if cert:
            return (True, "CertificateLog(PDPA)")
        # An unscannable site can never earn a certificate, so "no
        # CertificateLog" would flag it forever as a silent strand. If the buyer
        # was told and refunded, the order IS closed out — just not with a
        # deliverable. Keep it listed (fulfillable stays false) but say so, or
        # the genuine strands get lost in the noise.
        if ad.get("inaccessible_notice_sent_at"):
            refunded = "refunded" if ad.get("refunded") else "REFUND NOT ISSUED"
            return (
                False,
                f"unscannable — buyer notified, {refunded} (no cert possible)",
            )
        return (False, "no PDPA CertificateLog")

    if report.framework == "compliance_notarization":
        sent = bool(ad.get("notarization_email_sent"))
        return (sent, "certificate emailed" if sent else "certificate never emailed")

    if report.framework == "vendor_proof":
        rec = (
            db.query(VerifyRecord)
            .filter(VerifyRecord.vendor_id == report.owner_id)
            .first()
        )
        return (bool(rec), "VerifyRecord" if rec else "no VerifyRecord")

    return (True, "unknown framework — not assessed")


# Which Celery task actually DELIVERS each framework. Note this is deliberately
# not process_report_task: re-running the scan does not re-send the deliverable,
# and for a report that already scanned fine it burns a full AI run for nothing.
def _delivery_task_for(framework: str):
    from app.workers.tasks import (
        fulfill_notarization_task,
        fulfill_pdpa_task,
        fulfill_vendor_proof_task,
    )

    return {
        "pdpa_quick_scan": fulfill_pdpa_task,
        "vendor_proof": fulfill_vendor_proof_task,
        "compliance_notarization": fulfill_notarization_task,
    }.get(framework)


@router.get("/failed-reports")
def list_failed_reports(
    limit: int = Query(50, ge=1, le=200),
    _auth: bool = Depends(_admin_auth),
):
    """List reports stuck in ``status='failed'`` (newest first).

    Primary use: after a gas-wallet outage, the anchor step of
    ``process_report_task`` raises, the task exhausts its 3 retries, and the
    report lands in ``failed``. Top up gas, then requeue these from the panel
    via ``POST /admin/requeue-report``. ``missing_anchor`` flags the common
    case (report done but ``tx_hash`` never set), which is the gas signature.
    """
    from app.core.models import Report
    db = SessionLocal()
    try:
        rows = (
            db.query(Report)
            .filter(Report.status == "failed")
            .order_by(Report.created_at.desc())
            .limit(limit)
            .all()
        )
        out = []
        for r in rows:
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            out.append({
                "report_id": str(r.id),
                "framework": r.framework,
                "company_name": r.company_name,
                "contact_email": ad.get("contact_email") or ad.get("customer_email"),
                "product_type": ad.get("product_type"),
                "missing_anchor": r.tx_hash is None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return {"count": len(out), "reports": out}
    finally:
        db.close()


@router.post("/requeue-report")
def requeue_report(
    body: RequeueReportBody,
    _auth: bool = Depends(_admin_auth),
):
    """Re-run ``process_report_task`` for a failed/stuck report.

    Resets ``status`` to ``pending`` and re-queues the full workflow (scan →
    PDF → anchor). Idempotent-safe: a report already ``completed`` is left
    untouched so an accidental click can't clobber a delivered cert.
    """
    from app.core.models import Report
    from app.core.repositories.report_repository import ReportRepository
    db = SessionLocal()
    try:
        report = ReportRepository.get_by_id(db, str(body.report_id))
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        if report.status == "completed":
            raise HTTPException(
                status_code=409,
                detail="Report is already completed — refusing to re-run.",
            )
        report.status = "pending"
        db.commit()
        logger.info(f"[requeue-report] Reset report={body.report_id} to pending, requeuing")
    finally:
        db.close()

    from app.workers.tasks import process_report_task
    process_report_task.apply_async(kwargs={"report_id": body.report_id}, countdown=2)
    return {
        "ok": True,
        "report_id": body.report_id,
        "queued": True,
        "note": "Report requeued. It will re-run the scan, PDF and on-chain anchor.",
    }


@router.get("/stranded-fulfillments")
def list_stranded_fulfillments(
    limit: int = Query(50, ge=1, le=200),
    _auth: bool = Depends(_admin_auth),
):
    """Paid orders that were never actually delivered, whatever their status.

    Distinct from ``/failed-reports``, which only surfaces ``status='failed'``.
    A fulfillment can strand on a ``completed`` report: the scan succeeds, then
    the delivery step (PDF → CertificateLog → email) never runs. That was the
    2026-07-27 PDPA case — buyer charged, report ``completed``, nothing sent,
    and invisible to every existing admin view.

    ``fulfillable`` says whether ``POST /admin/refulfill`` can fix it now: false
    means the underlying scan never produced a score, so the scan has to be
    re-run first via ``POST /admin/requeue-report``.
    """
    from app.core.models import Report
    from app.services.pdpa_findings import resolve_pdpa_risk_score

    db = SessionLocal()
    try:
        rows = (
            db.query(Report)
            .filter(Report.framework.in_(_FULFILLMENT_FRAMEWORKS))
            .order_by(Report.created_at.desc())
            .limit(1000)
            .all()
        )
        out = []
        for r in rows:
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            if not ad.get("payment_confirmed"):
                continue
            delivered, evidence = _delivery_evidence(db, r)
            if delivered:
                continue
            fulfillable = (
                resolve_pdpa_risk_score(ad) is not None
                if r.framework == "pdpa_quick_scan"
                else True
            )
            out.append({
                "report_id": str(r.id),
                "framework": r.framework,
                "status": r.status,
                "company_name": r.company_name,
                "contact_email": ad.get("contact_email") or ad.get("customer_email"),
                "product_type": ad.get("product_type") or ad.get("bundle_source"),
                "stripe_session_id": ad.get("stripe_session_id"),
                "missing": evidence,
                "fulfillable": fulfillable,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
            if len(out) >= limit:
                break
        return {"count": len(out), "reports": out}
    finally:
        db.close()


@router.post("/refulfill")
def refulfill_report(
    body: RefulfillBody,
    _auth: bool = Depends(_admin_auth),
):
    """Re-run DELIVERY for a paid report that was never fulfilled.

    Queues the framework's ``fulfill_*`` task — not ``process_report_task``.
    Re-running the scan would not re-send the deliverable and would burn a full
    AI run on a report that already scanned fine.

    Refuses when delivery evidence already exists, so an accidental click can't
    double-charge gas, duplicate a CertificateLog or re-email the buyer.
    """
    from app.core.models import Report

    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == body.report_id).first()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        task = _delivery_task_for(report.framework)
        if task is None:
            raise HTTPException(
                status_code=400,
                detail=f"No delivery task for framework '{report.framework}'",
            )

        delivered, evidence = _delivery_evidence(db, report)
        if delivered:
            raise HTTPException(
                status_code=409,
                detail=f"Already delivered ({evidence}) — refusing to re-fulfil.",
            )

        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        contact_email = ad.get("contact_email") or ad.get("customer_email")
        task_name = task.name
        report_id = str(report.id)
    finally:
        db.close()

    task.delay(report_id, contact_email)
    logger.info(f"[refulfill] queued {task_name} for report={report_id}")
    return {
        "ok": True,
        "report_id": report_id,
        "queued": task_name,
        "contact_email": contact_email,
        "note": "Delivery requeued. The buyer will receive their deliverable by email.",
    }


@router.post("/refund")
def refund_purchase(
    body: RefundBody,
    _auth: bool = Depends(_admin_auth),
):
    """Manually refund a purchase we could not fulfil.

    The automated path (``_deliver_unscannable_and_refund``) already refunds
    terminal-state scans. This is the lever for everything it skipped: a refund
    that errored, a support ticket, a scan that technically completed but was
    worthless to the buyer.

    It goes through the same ``refund_for_session``, so the cache claim and the
    Stripe ``idempotency_key`` cover this entry point too — clicking twice, or
    clicking after the worker already refunded, cannot move money twice.
    """
    from app.core.models import Report
    from app.services.refund_service import (
        refund_for_session,
        refund_summary_for_assessment,
    )

    if not body.report_id and not body.session_id:
        raise HTTPException(status_code=400, detail="report_id or session_id is required")

    db = SessionLocal()
    report = None
    try:
        session_id = body.session_id
        if body.report_id:
            report = db.query(Report).filter(Report.id == body.report_id).first()
            if not report:
                raise HTTPException(status_code=404, detail="Report not found")
            ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
            session_id = session_id or ad.get("stripe_session_id")
            if not session_id:
                raise HTTPException(
                    status_code=400,
                    detail="Report has no stripe_session_id — pass session_id explicitly",
                )

        result = refund_for_session(session_id, reason=body.reason)

        # Persist onto the report when we have one, so the refund is visible in
        # /admin/stranded-fulfillments rather than only in Stripe.
        if report is not None:
            from sqlalchemy.orm.attributes import flag_modified
            from app.workers.tasks import _set_assessment_values

            _set_assessment_values(report, refund_summary_for_assessment(result))
            flag_modified(report, "assessment_data")
            db.commit()
    finally:
        db.close()

    logger.info(
        f"[admin-refund] session={session_id} refunded={result['refunded']} "
        f"skipped={result['skipped_reason']} reason={body.reason!r}"
    )
    return {"ok": True, "session_id": session_id, **result}


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
        "DiscoveredVendor registry match + live ACRA lookup on the certificate. "
        "For an account-subject run it is also resolved against ACRA and, on a "
        "match, persisted to the account, so subscription SKUs (MAS TRM Suite) "
        "render a registered legal name and UEN instead of the [UEN] placeholder. "
        "No match means the account stays unverified — nothing is invented.",
    )
    managed_entity_id: Optional[str] = Field(
        default=None,
        description="Subject entity id for a CSP/DPO order. When set, the report's "
        "identity is resolved from that row and the account's own company/website "
        "are left untouched — the same precedence a real checkout applies.",
    )
    managed_entity_name: Optional[str] = Field(
        default=None,
        description="QA convenience: name a managed entity instead of pasting an id. "
        "Reuses the test account's existing entity of that name, or registers one, "
        "then verifies it against ACRA through the production code path.",
    )
    industry: Optional[str] = Field(default=None, description="Optional industry for sector scoping")
    sector: Optional[str] = Field(default=None, description="Optional sector for Tender Intelligence scoping")
    rfp_description: Optional[str] = Field(default=None)
    force_resend: bool = Field(
        default=False,
        description="Subscriptions only: re-fire activation side effects (welcome "
        "email, first-cycle deliverables) even if this test email already "
        "activated this SKU. Off by default so a double-click doesn't double-send.",
    )

    @field_validator("company_name", mode="before")
    @classmethod
    def validate_names(cls, v):
        # Allow default "Booppa QA" to pass through unflagged since it's an explicit default
        # for admin test simulation, but reject purely obvious placeholders.
        if v == "Booppa QA":
            return v
        return validate_name_field(v)


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
    entity_id = (body.managed_entity_id or "").strip()
    entity_name = (body.managed_entity_name or "").strip()
    # A named subject means this order is about a client company, not about the QA
    # account. Everything downstream keys off this flag: the account's own
    # company/website are never written, and identity comes from the entity row.
    subject_is_entity = bool(entity_id or entity_name)
    resolved_entity_id: Optional[str] = None
    entity_identity: Optional[dict] = None
    account_identity: Optional[dict] = None
    rfp_description = (body.rfp_description or "").strip() or DEFAULT_QA_RFP_BRIEF
    sim_id = f"admin-sim-{_uuid.uuid4()}"
    # The simulated *subscription* id is deterministic per (email, SKU), unlike
    # sim_id — which doubles as a checkout session id for one-time products and
    # must stay unique so each run gets its own stub Report.
    #
    # Why: `_activate_subscription` claims a once-per-subscription slot in Redis
    # (`sub_activated:{stripe_subscription_id}`) to stop duplicate Stripe events
    # double-sending the welcome email and double-queueing first-cycle work. A
    # fresh uuid4 per click made that claim vacuous for QA, so two clicks on the
    # Suite test checkout sent two identical "MAS TRM Baseline is ready" emails.
    # Deterministic id ⇒ the existing guard dedupes repeat clicks too.
    _sim_sub_seed = f"{customer_email}|{product_type}".encode()
    sim_sub_id = f"admin-sim-{_hashlib.sha256(_sim_sub_seed).hexdigest()[:24]}"

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
                # A new QA account created for an entity-subject run gets neutral
                # placeholders: the typed company belongs to the client entity, and
                # seeding it onto the account would recreate the exact "account
                # identity stands in for the subject" confusion this run is testing.
                company="Booppa QA" if subject_is_entity else (company_name or "Booppa QA"),
                website="https://booppa.io" if subject_is_entity else (vendor_url or "https://booppa.io"),
                is_active=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"[simulate-purchase] Created test user {customer_email}")
        else:
            # Use the ACTIVE test-checkout input: when the admin supplies a
            # company/website for this run, it overwrites whatever stale value a
            # reused QA account is carrying (a prior run's identity must not mask
            # what we're testing now). Empty form fields leave the existing value
            # untouched. Mirrors the report-scoped resolution fix.
            #
            # Skipped entirely for an entity-subject run: the company and website
            # on that run describe the client company, not the firm placing the
            # order, and stamping them onto the account is the write this whole
            # architecture change exists to stop.
            updated = False
            if not subject_is_entity:
                if vendor_url and user.website != vendor_url:
                    user.website = vendor_url; updated = True
                if company_name and user.company != company_name:
                    user.company = company_name; updated = True
                # If cached identity conflicts with company_name, clear it so test checkout
                # never serves a stale legal_name from an earlier run.
                from app.services.evidence_enricher import _norm
                if company_name and (
                    (user.legal_name_hint and _norm(user.legal_name_hint) != _norm(company_name))
                    or (user.legal_name and _norm(user.legal_name) != _norm(company_name))
                ):
                    updated = True
                sim_industry = (body.industry or body.sector or "").strip()
                if sim_industry and user.industry != sim_industry:
                    user.industry = sim_industry; updated = True
            if updated:
                from app.services.evidence_enricher import clear_cached_identity

                clear_cached_identity(user, db, commit=False)
                db.commit()

        # Account-subject UEN: resolve it against the live ACRA registry and, on a
        # match, persist through the single sanctioned write path. Until now the
        # typed UEN only rode along in Stripe-style metadata, which reaches the stub
        # Report — so one-time SKUs saw it and *subscription* SKUs did not. That is
        # why a Suite test checkout produced a MAS TRM pack whose cover read
        # "UEN [UEN]": nothing had ever written an identity onto the account.
        #
        # Deliberately not a blind write of whatever was typed. `resolve_legal_name`
        # queries data.gov.sg and only records what the registry actually returns,
        # so a wrong or invented number leaves the account unverified rather than
        # stamping a fabricated registration onto a certified deliverable.
        if uen and not subject_is_entity:
            from app.services.evidence_enricher import (resolve_legal_name,
                                                        trusted_cached_uen)

            try:
                _hint = company_name or user.company
                _site = vendor_url or user.website
                acra_legal_name = await resolve_legal_name(
                    user, db, company_hint=_hint, uen=uen, website_hint=_site,
                )
                db.refresh(user)
                # Read back through the trust gate, not off the column: a UEN whose
                # provenance doesn't match this run's company belongs to whatever
                # the account was doing last time, and reporting it as "verified"
                # here would be the same lie one layer up.
                confirmed_uen = trusted_cached_uen(
                    user, company_hint=_hint, website_hint=_site,
                )
                account_identity = {
                    "uen": confirmed_uen,
                    "legal_name": acra_legal_name,
                    "verified": bool(confirmed_uen),
                }
                if not confirmed_uen:
                    logger.warning(
                        "[simulate-purchase] ACRA had no match for uen=%s company=%r "
                        "— account left unverified", uen, company_name,
                    )
            except Exception as _acra_err:
                # Identity enrichment is not what this test checkout is proving;
                # a registry outage must not block the fulfillment run under test.
                logger.warning("[simulate-purchase] ACRA resolution failed: %s", _acra_err)
                db.rollback()

        sim_sector = (body.industry or body.sector or getattr(user, "industry", None) or "").strip()
        if sim_sector:
            try:
                from app.services.tender_service import set_vendor_sector
                set_vendor_sector(db, user.id, sim_sector)
            except Exception as _sec_err:
                logger.warning("[simulate-purchase] Failed to sync VendorSector: %s", _sec_err)

        # Resolve the subject entity, if this run names one. Done here, inside the
        # session that just guaranteed the user exists, because a ManagedEntity is
        # owned by an account and cannot be looked up before one is there.
        if subject_is_entity:
            from app.core.models import ManagedEntity
            from app.services.evidence_enricher import resolve_managed_entity_identity

            q = db.query(ManagedEntity).filter(
                ManagedEntity.owner_user_id == user.id,
                ManagedEntity.status == "ACTIVE",
            )
            entity = (
                q.filter(ManagedEntity.id == entity_id).first()
                if entity_id
                else q.filter(ManagedEntity.company_name == entity_name).first()
            )
            if entity is None and entity_id:
                # An id that resolves to nothing is a real 422 in checkout too. Never
                # invent a row for it — that would hide the failure we are testing.
                raise HTTPException(
                    status_code=422,
                    detail="managed_entity_id is not an active entity for this test account.",
                )
            if entity is None:
                # Name-only: register it, so QA can exercise the entity path without
                # first walking the CSP UI. Verification below is the real one.
                entity = ManagedEntity(
                    owner_user_id=user.id,
                    company_name=entity_name,
                    website=vendor_url or None,
                    status="ACTIVE",
                )
                db.add(entity)
                db.commit()
                db.refresh(entity)
                logger.info(
                    "[simulate-purchase] Registered managed entity %s for %s",
                    entity_name, customer_email,
                )

            # Same helper the live checkout calls — ACRA match plus provenance
            # write-back through record_verified_identity, on the entity row.
            resolved = await resolve_managed_entity_identity(entity, db)
            db.refresh(entity)

            resolved_entity_id = str(entity.id)
            # The entity, not the form, now defines the subject of this order.
            company_name = (entity.company_name or "").strip() or company_name
            vendor_url = (entity.website or "").strip() or vendor_url
            if resolved.get("uen"):
                uen = resolved["uen"]
            entity_identity = {
                "managed_entity_id": resolved_entity_id,
                "company_name": entity.company_name,
                "legal_name": resolved.get("legal_name"),
                "uen": resolved.get("uen"),
                # False here is the honest outcome, not an error: the report will
                # read "Not verified" rather than borrow the QA account's identity.
                "verified": bool(resolved.get("verified")),
            }
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
    if resolved_entity_id:
        # Fulfillment re-reads the entity by id rather than trusting the company
        # name string, so the test deliverable names the row we just verified —
        # exactly what a real CSP order carries in its Stripe metadata.
        metadata["managed_entity_id"] = resolved_entity_id
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
        # "Force resend" releases the once-per-subscription claim so the same QA
        # email can deliberately re-receive the activation set. Without it the
        # claim stands and a repeat click is a no-op on side effects.
        if body.force_resend:
            try:
                from app.core.cache import cache as _cache
                # Only the subscription claim needs releasing — the TRM baseline's
                # own 24h lock is already skipped on the test path via
                # bypass_idempotency=test_simulation.
                _cache.delete(_cache.cache_key(f"sub_activated:{sim_sub_id}"))
            except Exception as exc:
                logger.warning("[simulate-purchase] force_resend cache clear failed: %s", exc)
        # Peek at the guard BEFORE activating so the response can say whether
        # side effects actually fired. Without this the endpoint returns a
        # cheerful 200 for a run that sent no email and queued no work, and the
        # only trace is a worker log line — which reads to the tester as
        # "fulfillment is broken" rather than "this was deduped". Safe to peek:
        # this is the single-caller QA tool, and `activate_subscription` still
        # does the atomic SET NX claim, so a race can only make this optimistic.
        suppressed = False
        if not body.force_resend:
            try:
                from app.core.cache import cache as _cache
                suppressed = _cache.get(
                    _cache.cache_key(f"sub_activated:{sim_sub_id}")
                ) is not None
            except Exception as exc:
                logger.warning("[simulate-purchase] guard peek failed: %s", exc)

        await activate_subscription(
            product_type=product_type,
            customer_email=customer_email,
            stripe_subscription_id=sim_sub_id,
            stripe_customer_id=sim_sub_id,
            test_simulation=True,
            demo=is_buyer,
            # Test Identity drives first-cycle deliverables (Vendor snapshot,
            # PDPA Monitor report) without mutating the real user profile.
            override_company=company_name or None,
            override_website=vendor_url or None,
            # Releasing the subscription claim above is necessary but NOT
            # sufficient: the PDPA rescan has its own same-day lock and 24h DB
            # check, and the Monitor Report, drift check and quarterly snapshot
            # are all queued from inside it. Without this a same-day re-test
            # delivered the digest alone.
            force_rescan=body.force_resend,
        )
        db = SessionLocal()
        try:
            u = UserRepository.get_by_email(db, customer_email)
            from app.core.repositories.subscription_repository import SubscriptionRepository
            sub = SubscriptionRepository.get_by_stripe_subscription_id(db, sim_sub_id)
            details = {
                "plan": getattr(u, "plan", None),
                "subscription_id": str(sub.id) if sub else None,
                "stripe_subscription_id": sim_sub_id,
                "force_resend": body.force_resend,
                # The entitlement grant is idempotent and always applies; this
                # flag is specifically about the activation email + first-cycle
                # deliverable fan-out, which is what a tester is waiting for.
                "side_effects_fired": not suppressed,
            }
            if suppressed:
                details["warning"] = (
                    f"{customer_email} already activated {product_type} within the last "
                    "24h, so NO email was sent and NO deliverables were queued. The plan "
                    "entitlement is still applied. Re-send with \"force_resend\": true."
                )
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
            from app.core.repositories.report_repository import ReportRepository
            stubs = ReportRepository.list_by_stripe_session_id(db, sim_id)
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
            from app.core.repositories.report_repository import ReportRepository
            stub_list = ReportRepository.list_by_stripe_session_id(db, sim_id)
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
        # Present only for entity-subject runs. Lets the test-checkout page show
        # which company the deliverable is actually about and whether ACRA
        # confirmed it — the two facts a QA run of this path exists to check.
        "subject_identity": entity_identity,
        # Present only when a UEN was supplied for an account-subject run.
        # `verified: false` means ACRA returned nothing and the deliverable will
        # show the placeholder — not that the lookup was skipped.
        "account_identity": account_identity,
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


class TrmDemoBaselineRequest(BaseModel):
    customer_email: str = Field(..., description="Recipient — receives the real baseline email")
    company_name: Optional[str] = Field(default="NovaPay Fintech Pte Ltd")
    uen: Optional[str] = Field(default=None, description="Optional Singapore UEN for ACRA legal-name resolution")
    live_ai: bool = Field(
        default=True,
        description="Use live DeepSeek gap analysis when a key is configured; "
        "false forces the deterministic seeded narratives.",
    )

    @field_validator("company_name", mode="before")
    @classmethod
    def validate_names(cls, v):
        if v in (None, "", "NovaPay Fintech Pte Ltd"):
            return v or "NovaPay Fintech Pte Ltd"
        return validate_name_field(v)


# Defined `def`, not `async def`, on purpose: the baseline worker bridges to async
# internally via asyncio.run(), which raises inside a live event loop. FastAPI runs
# sync endpoints in a threadpool, so there is no running loop here.
@router.post("/trm/demo-baseline")
def admin_trm_demo_baseline(
    body: TrmDemoBaselineRequest,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Seed the evidence-graded MAS TRM demo tenant and regenerate its baseline.

    Same code path as `scripts/demo_trm_baseline.py` — the point is that the
    tested-vs-documented artifact is reproducible by clicking a button rather
    than by having shell access to a box.
    """
    from app.services.trm_demo_harness import seed_and_generate

    db = SessionLocal()
    try:
        result = seed_and_generate(
            customer_email=body.customer_email,
            company_name=body.company_name or "NovaPay Fintech Pte Ltd",
            uen=body.uen,
            live_ai=body.live_ai,
            db=db,
        )
    except Exception as exc:
        logger.exception("[AdminTRMDemo] baseline generation failed")
        raise HTTPException(status_code=500, detail=f"Demo baseline failed: {exc}")
    finally:
        db.close()

    result.pop("pdf_bytes", None)
    return {"success": True, **result}


@router.get("/trm/demo-baseline/{user_id}")
def admin_trm_demo_baseline_latest(
    user_id: str,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Re-presign the latest baseline for a demo user (presigns expire in 7 days)."""
    from app.services.trm_demo_harness import latest_baseline_url

    db = SessionLocal()
    try:
        url = latest_baseline_url(user_id, db)
    finally:
        db.close()
    if not url:
        return {"available": False}
    return {"available": True, "download_url": url}


class TrmDemoDocumentPackRequest(BaseModel):
    mode: str = Field(
        default="matrix",
        description="'matrix' runs all three acceptance cases; 'single' runs one tenant.",
    )
    customer_email: Optional[str] = Field(default=None)
    company_name: Optional[str] = Field(default="NovaPay Fintech Pte Ltd")
    uen: Optional[str] = None
    mas_licence_type: Optional[str] = Field(
        default=None,
        description=(
            "MAS licence key (bank | merchant_bank | mpi | spi | insurer | "
            "capital_markets | other_fi). Omit to exercise the unconfirmed case, "
            "which gates the Outsourcing Risk Register."
        ),
    )
    live_ai: bool = Field(default=True)

    @field_validator("company_name")
    @classmethod
    def validate_names(cls, v):
        if v in (None, "", "NovaPay Fintech Pte Ltd"):
            return v or "NovaPay Fintech Pte Ltd"
        return validate_name_field(v)


# Sync `def` for the same reason as the baseline endpoint above: the pack worker
# bridges to async via asyncio.run(), which raises inside a live event loop.
@router.post("/trm/demo-document-pack")
def admin_trm_demo_document_pack(
    body: TrmDemoDocumentPackRequest,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Run the MAS TRM document-pack acceptance matrix (or a single tenant).

    Same code path as `scripts/demo_trm_document_pack.py`. PDF bytes are
    stripped from the response — this returns the verification readout (branch,
    status, per-document hashes), not a 7-PDF JSON payload. Use the script when
    you need the files themselves.

    Nothing is emailed and nothing is anchored: the harness stubs S3, the chain
    and the mail provider, so this cannot mail a demo pack to a real customer.
    """
    from app.services.trm_demo_harness import (run_acceptance_matrix,
                                               seed_and_generate_document_pack)

    db = SessionLocal()
    try:
        if body.mode == "matrix":
            result = run_acceptance_matrix(live_ai=body.live_ai, db=db)
        else:
            result = {"cases": [seed_and_generate_document_pack(
                customer_email=body.customer_email,
                company_name=body.company_name or "NovaPay Fintech Pte Ltd",
                uen=body.uen,
                mas_licence_type=body.mas_licence_type,
                live_ai=body.live_ai,
                db=db,
            )]}
    except Exception as exc:
        logger.exception("[AdminTRMDemo] document pack generation failed")
        raise HTTPException(status_code=500, detail=f"Demo document pack failed: {exc}")
    finally:
        db.close()

    for case in result.get("cases", []):
        for entry in (case.get("documents") or {}).values():
            entry.pop("pdf_bytes", None)
    return {"success": True, **result}


class ProSuiteDemoRequest(BaseModel):
    customer_email: str = Field(..., description="Recipient — the Pro tenant to activate")
    company_name: Optional[str] = Field(default="NovaPay Group Pte Ltd")
    subsidiary_names: Optional[list[str]] = Field(
        default=None,
        description="Override the two default subsidiary display names (positional).",
    )
    live_ai: bool = Field(
        default=True,
        description="Live gap analysis when a key is configured; false forces seeded narratives.",
    )

    @field_validator("company_name", mode="before")
    @classmethod
    def validate_names(cls, v):
        if v in (None, "", "NovaPay Group Pte Ltd"):
            return v or "NovaPay Group Pte Ltd"
        return validate_name_field(v)


# All three Pro Suite endpoints are `def`, not `async def`, on purpose: the harness
# reuses the baseline worker (which bridges to async via asyncio.run()) and
# `run_sso_roundtrip` drives a TestClient — both raise inside a live event loop.
# FastAPI runs sync endpoints in a threadpool, so there is no running loop here.
@router.post("/pro-suite/demo")
def admin_pro_suite_demo(
    body: ProSuiteDemoRequest,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Activate all four Pro-exclusive capabilities on a demo tenant and regenerate
    its branded baseline.

    Same code path as `scripts/demo_pro_suite.py`. The point is that "Ready → Active"
    for multi-subsidiary, white-label and SSO is reproducible by clicking a button,
    with real artifacts, rather than being asserted in a document.
    """
    from app.services.pro_suite_demo_harness import activate_pro_features

    db = SessionLocal()
    try:
        # capture_pdf=False so the baseline lands in S3 and the panel gets a
        # download_url. with_mock_idp seeds a valid SsoConfig; the sso-roundtrip
        # endpoint mints its own fresh IdP, so we clean the temp dir up here.
        result = activate_pro_features(
            customer_email=body.customer_email,
            company_name=body.company_name or "NovaPay Group Pte Ltd",
            subsidiary_names=body.subsidiary_names,
            live_ai=body.live_ai,
            capture_pdf=False,
            db=db,
        )
    except Exception as exc:
        logger.exception("[AdminProSuite] activation failed")
        raise HTTPException(status_code=500, detail=f"Pro Suite demo failed: {exc}")
    finally:
        db.close()

    if result.get("mock_idp_dir"):
        import shutil
        shutil.rmtree(result["mock_idp_dir"], ignore_errors=True)
    result.pop("pdf_bytes", None)
    result.pop("mock_idp_dir", None)
    return {"success": True, **result}


@router.get("/pro-suite/demo/{user_id}/rollup")
def admin_pro_suite_rollup(
    user_id: str,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Group-level TRM rollup across the demo tenant and its subsidiaries, without
    an authenticated vendor session — the consolidated view Gianpaolo's (a) asked for."""
    from app.api.vendor_features import build_subsidiary_comparison
    from app.core.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="No such tenant")
        if user.parent_user_id is not None:
            raise HTTPException(
                status_code=400, detail="Pass the parent tenant, not a subsidiary."
            )
        return build_subsidiary_comparison(db, user)
    finally:
        db.close()


@router.post("/pro-suite/demo/{user_id}/sso-roundtrip")
def admin_pro_suite_sso_roundtrip(
    user_id: str,
    tamper: bool = False,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Drive a signed SAML assertion through the real ACS route and report the
    verdict. `tamper=true` mints a corrupted assertion, which must be rejected."""
    from app.services.pro_suite_demo_harness import run_sso_roundtrip

    db = SessionLocal()
    try:
        return run_sso_roundtrip(user_id=user_id, tamper=tamper, db=db)
    except Exception as exc:
        logger.exception("[AdminProSuite] SSO round trip failed")
        raise HTTPException(status_code=500, detail=f"SSO round trip failed: {exc}")
    finally:
        db.close()


class CspDemoRequest(BaseModel):
    customer_email: str = Field(..., description="Recipient — the CSP tenant to onboard")
    company_name: Optional[str] = Field(default=None)

    @field_validator("company_name", mode="before")
    @classmethod
    def _clean_company(cls, v):
        if v in (None, ""):
            return v
        return validate_name_field(v)


# `def`, not `async def`: the harness renders the Day-1 baseline via a helper that
# bridges the ACRA lookup with asyncio.run(), which raises inside a live event
# loop. FastAPI runs sync endpoints in a threadpool, so there is no running loop.
@router.post("/csp/demo")
def admin_csp_demo(
    body: CspDemoRequest,
    _auth: bool = Depends(_admin_auth),
) -> dict:
    """Walk a demo CSP org through onboarding and produce its inspection records.

    Same code path as `scripts/demo_csp_onboarding.py`. Returns the Day-1 baseline
    plus the nominee fit-and-proper record and the STR decision record (with their
    download URLs), so the actual onboarding output can be judged — not described.
    """
    from app.services.csp_demo_harness import run_csp_onboarding_demo

    db = SessionLocal()
    try:
        result = run_csp_onboarding_demo(
            customer_email=body.customer_email,
            company_name=body.company_name,
            capture_pdfs=False,
            db=db,
        )
    except Exception as exc:
        logger.exception("[AdminCsp] onboarding demo failed")
        raise HTTPException(status_code=500, detail=f"CSP onboarding demo failed: {exc}")
    finally:
        db.close()

    return {"success": True, **result}


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
