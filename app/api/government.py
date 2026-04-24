"""
Government Portal API
=====================
Dedicated endpoints for Singapore government procurement officers accessing
the Booppa Vendor Intelligence portal.

All /api/government/* routes are public-read (no auth required) so procurement
officers can use the portal without creating a Booppa account.  Writes (verify
by UEN) trigger a lightweight ACRA + blockchain lookup only.

Endpoints
---------
GET  /api/government/vendors          — paginated vendor list with trust signals
GET  /api/government/vendors/{uen}    — single vendor detail by UEN
GET  /api/government/tenders          — live open GeBIZ tenders (latest first)
POST /api/government/verify           — verify vendor by UEN or blockchain TX hash
POST /api/government/shortlist-report — generate plain-text evaluation report
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import User
from app.core.models_v6 import VendorScore, VendorSector
from app.core.models_v8 import VendorStatusSnapshot
from app.core.models_v10 import DiscoveredVendor, MarketplaceVendor
from app.core.models_gebiz import GebizTender
from app.core.auth import (
    authenticate_user, register_user,
    create_access_token, create_refresh_token,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ── helpers ───────────────────────────────────────────────────────────────────

_DEPTH_RANK = {"UNVERIFIED": 0, "BASIC": 1, "STANDARD": 2, "DEEP": 3, "CERTIFIED": 4}


def _vendor_row(
    user: User,
    score: Optional[VendorScore],
    snapshot: Optional[VendorStatusSnapshot],
    sector: Optional[VendorSector],
) -> dict:
    trust_score = score.total_score if score else 0
    depth       = snapshot.verification_depth if snapshot else "UNVERIFIED"
    risk        = snapshot.risk_signal        if snapshot else "CLEAN"
    readiness   = snapshot.procurement_readiness if snapshot else "NEEDS_ATTENTION"
    percentile  = round(snapshot.risk_adjusted_pct) if (snapshot and snapshot.risk_adjusted_pct) else None

    # Normalise readiness enum → frontend keys
    readiness_map = {
        "READY":           "READY",
        "CONDITIONAL":     "CONDITIONAL",
        "NEEDS_ATTENTION": "NEEDS_ATTENTION",
        "NOT_READY":       "NEEDS_ATTENTION",
    }
    risk_map = {
        "CLEAN":    "CLEAN",
        "WATCH":    "WATCH",
        "FLAGGED":  "FLAGGED",
        "CRITICAL": "FLAGGED",
    }

    return {
        "id":             str(user.id),
        "name":           user.company or "Unknown",
        "uen":            getattr(user, "uen", None) or "",
        "industry":       sector.sector if sector else "General",
        "trust_score":    trust_score,
        "depth":          depth,
        "risk":           risk_map.get(risk, "CLEAN"),
        "readiness":      readiness_map.get(readiness, "NEEDS_ATTENTION"),
        "percentile":     percentile,
        "description":    getattr(user, "company_description", None) or "",
        "verified":       depth in ("DEEP", "CERTIFIED"),
        "website":        getattr(user, "website", None) or "",
    }


# ── POST /register ───────────────────────────────────────────────────────────

class GovRegisterRequest(BaseModel):
    email:      str
    password:   str
    full_name:  Optional[str] = None
    agency:     Optional[str] = None   # stored as `company`


class GovAuthOut(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    role:          str = "GOVERNMENT"
    email:         str
    full_name:     Optional[str] = None
    agency:        Optional[str] = None


@router.post("/register", response_model=GovAuthOut, status_code=201)
async def gov_register(body: GovRegisterRequest, db: Session = Depends(get_db)):
    """
    Register a government procurement officer account.

    NOTE: .gov.sg domain check is disabled for testing.
    To enable, uncomment the domain validation block below.
    """
    # -- Uncomment to enforce .gov.sg domain in production --
    # domain = body.email.rsplit("@", 1)[-1].lower()
    # if not domain.endswith(".gov.sg"):
    #     raise HTTPException(
    #         status_code=422,
    #         detail="Access is restricted to Singapore government email addresses (.gov.sg).",
    #     )

    try:
        user = register_user(
            db,
            email=body.email,
            password=body.password,
            company=body.agency or "",
            role="GOVERNMENT",
        )
        if body.full_name:
            user.full_name = body.full_name
            db.commit()
            db.refresh(user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    return GovAuthOut(
        access_token=access_token,
        refresh_token=refresh_token,
        email=user.email,
        full_name=user.full_name,
        agency=user.company or None,
    )


# ── POST /login ───────────────────────────────────────────────────────────────

class GovLoginRequest(BaseModel):
    email:    str
    password: str


@router.post("/login", response_model=GovAuthOut)
async def gov_login(body: GovLoginRequest, db: Session = Depends(get_db)):
    """Authenticate a government portal user."""
    user = authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if getattr(user, "role", "") != "GOVERNMENT":
        raise HTTPException(status_code=403, detail="This login is for government accounts only.")

    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    return GovAuthOut(
        access_token=access_token,
        refresh_token=refresh_token,
        email=user.email,
        full_name=getattr(user, "full_name", None),
        agency=getattr(user, "company", None) or None,
    )


# ── GET /vendors ──────────────────────────────────────────────────────────────

@router.get("/vendors")
def list_vendors(
    q:        Optional[str] = Query(None, description="Search by name or UEN"),
    industry: Optional[str] = Query(None, description="Filter by industry/sector"),
    min_score: int          = Query(0, ge=0, le=100),
    depth:    Optional[str] = Query(None, description="UNVERIFIED|BASIC|STANDARD|DEEP|CERTIFIED"),
    readiness:Optional[str] = Query(None, description="READY|CONDITIONAL|NEEDS_ATTENTION"),
    page:     int           = Query(1, ge=1),
    per_page: int           = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return paginated vendor list with all procurement intelligence fields."""
    base = (
        db.query(User, VendorScore, VendorStatusSnapshot, VendorSector)
        .outerjoin(VendorScore,          VendorScore.vendor_id          == User.id)
        .outerjoin(VendorStatusSnapshot, VendorStatusSnapshot.vendor_id == User.id)
        .outerjoin(VendorSector,         VendorSector.vendor_id         == User.id)
        .filter(User.is_active == True, User.role == "VENDOR")
    )

    if q:
        like = f"%{q}%"
        base = base.filter(
            or_(User.company.ilike(like), User.uen.ilike(like))  # type: ignore[union-attr]
        )
    if industry and industry != "All":
        base = base.filter(VendorSector.sector == industry)
    if depth:
        base = base.filter(VendorStatusSnapshot.verification_depth == depth)
    if readiness:
        base = base.filter(VendorStatusSnapshot.procurement_readiness == readiness)
    if min_score > 0:
        base = base.filter(VendorScore.total_score >= min_score)

    total = base.count()
    rows  = (
        base.order_by(VendorScore.total_score.desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "vendors":  [_vendor_row(u, s, snap, sec) for u, s, snap, sec in rows],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
    }


# ── GET /vendors/{uen} ────────────────────────────────────────────────────────

@router.get("/vendors/{uen}")
def get_vendor_by_uen(uen: str, db: Session = Depends(get_db)):
    """Return full vendor profile by UEN."""
    user = db.query(User).filter(User.uen == uen, User.is_active == True).first()  # type: ignore[union-attr]
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found")

    score    = db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
    snapshot = db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id == user.id).first()
    sector   = db.query(VendorSector).filter(VendorSector.vendor_id == user.id).first()

    row = _vendor_row(user, score, snapshot, sector)

    # Enrich with GeBIZ history from DiscoveredVendor
    disc = db.query(DiscoveredVendor).filter(DiscoveredVendor.uen == uen).first()
    if disc:
        row["gebiz_supplier"]       = bool(disc.gebiz_supplier)
        row["gebiz_contracts_count"]= disc.gebiz_contracts_count or 0
        row["gebiz_total_value"]    = disc.gebiz_total_value or 0.0

    return row


# ── GET /tenders ──────────────────────────────────────────────────────────────

@router.get("/tenders")
def list_tenders(
    limit: int = Query(8, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Return open GeBIZ tenders sorted by closing date (soonest first).
    Uses the same on-demand sync as the GeBIZ ticker so data is always fresh.
    """
    from datetime import timezone as _tz
    from app.services.gebiz_service import ensure_tenders_loaded

    try:
        ensure_tenders_loaded(db)
    except Exception as exc:
        logger.warning(f"[Government/Tenders] On-demand sync failed: {exc}")

    now = datetime.now(_tz.utc)
    tenders = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .filter(
            (GebizTender.closing_date == None) | (GebizTender.closing_date >= now)  # noqa: E711
        )
        .order_by(GebizTender.closing_date.asc().nullslast())
        .limit(limit)
        .all()
    )

    # Fallback mock data only when DB is truly empty after sync attempt
    if not tenders:
        return {"tenders": _mock_tenders(), "source": "mock"}

    def _fmt_value(v: Optional[float]) -> str:
        if not v:
            return "TBC"
        if v >= 1_000_000:
            return f"S${v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"S${v/1_000:.0f}K"
        return f"S${v:,.0f}"

    def _fmt_closing(dt: Optional[datetime]) -> str:
        if not dt:
            return "—"
        return dt.strftime("%-d %b %Y")

    return {
        "tenders": [
            {
                "agency":   t.agency or "—",
                "ref":      t.tender_no,
                "title":    t.title,
                "value":    _fmt_value(t.estimated_value),
                "closing":  _fmt_closing(t.closing_date),
                "url":      t.url or None,
            }
            for t in tenders
        ],
        "source": "live",
    }


def _mock_tenders() -> list:
    return [
        {"agency": "Ministry of Health",           "ref": "MOH/IT/2026/041", "title": "Electronic Health Records System Upgrade",     "value": "S$4.2M",  "closing": "30 May 2026", "url": None},
        {"agency": "Land Transport Authority",     "ref": "LTA/DIG/2026/018","title": "Predictive Analytics Platform for Traffic",    "value": "S$2.8M",  "closing": "15 Jun 2026", "url": None},
        {"agency": "Ministry of Education",        "ref": "MOE/ICT/2026/055","title": "Cybersecurity Monitoring Solution",            "value": "S$1.5M",  "closing": "22 Jun 2026", "url": None},
        {"agency": "CPF Board",                    "ref": "CPFB/IT/2026/009","title": "Cloud Infrastructure Migration — Phase 3",     "value": "S$6.1M",  "closing": "8 Jul 2026",  "url": None},
        {"agency": "National Environment Agency",  "ref": "NEA/DT/2026/033", "title": "IoT Sensor Data Management Platform",         "value": "S$890K",  "closing": "14 Jul 2026", "url": None},
    ]


# ── GET /stats ───────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """Real counts for the landing page stats panel."""
    from datetime import timezone as _tz
    from app.core.models_v6 import VerifyRecord

    total_vendors = (
        db.query(User)
        .filter(User.is_active == True, User.role == "VENDOR")
        .count()
    )

    # Verified = DEEP or CERTIFIED verification depth
    verified_vendors = (
        db.query(VendorStatusSnapshot)
        .filter(VendorStatusSnapshot.verification_depth.in_(["DEEP", "CERTIFIED"]))
        .count()
    )

    # Fall back to VerifyRecord count if no snapshots yet
    if verified_vendors == 0:
        try:
            from app.core.models_v6 import LifecycleStatus
            verified_vendors = (
                db.query(VerifyRecord)
                .filter(VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE)
                .count()
            )
        except Exception:
            pass

    now = datetime.now(_tz.utc)
    open_tenders = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .filter(
            (GebizTender.closing_date == None) | (GebizTender.closing_date >= now)  # noqa: E711
        )
        .count()
    )

    # Discovered vendors from ACRA/GeBIZ data
    discovered = db.query(DiscoveredVendor).count()

    return {
        "total_vendors":    total_vendors,
        "verified_vendors": verified_vendors,
        "open_tenders":     open_tenders,
        "discovered":       discovered,
    }


# ── POST /verify ──────────────────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    uen:  Optional[str] = None
    hash: Optional[str] = None   # blockchain TX hash or Booppa report ID


@router.post("/verify")
async def verify_vendor(body: VerifyRequest, db: Session = Depends(get_db)):
    """
    Verify a vendor certificate by UEN or blockchain TX hash.
    Returns structured verification result for the portal modal.
    """
    if not body.uen and not body.hash:
        raise HTTPException(status_code=422, detail="Provide uen or hash")

    result: dict = {
        "verified":     False,
        "company":      None,
        "uen":          body.uen,
        "framework":    None,
        "trust_score":  None,
        "depth":        None,
        "tx_hash":      body.hash,
        "anchored_at":  None,
        "error":        None,
    }

    # ── Look up vendor by UEN ─────────────────────────────────────────────────
    if body.uen:
        user = db.query(User).filter(User.uen == body.uen, User.is_active == True).first()  # type: ignore[union-attr]
        if user:
            score    = db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
            snapshot = db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id == user.id).first()

            result.update({
                "verified":    True,
                "company":     user.company,
                "uen":         body.uen,
                "framework":   "Vendor Proof v2",
                "trust_score": score.total_score if score else None,
                "depth":       snapshot.verification_depth if snapshot else "UNVERIFIED",
                "anchored_at": snapshot.computed_at.strftime("%-d %b %Y, %H:%M SGT") if snapshot and snapshot.computed_at else None,
            })
            # Try to fetch blockchain anchor via CertificateLog
            try:
                from app.core.models_v10 import CertificateLog
                log = (
                    db.query(CertificateLog)
                    .filter(CertificateLog.vendor_id == user.id)
                    .order_by(CertificateLog.created_at.desc())
                    .first()
                )
                if log:
                    result["tx_hash"] = getattr(log, "tx_hash", None) or body.hash
            except Exception:
                pass
        else:
            # Not a registered vendor — check DiscoveredVendor
            disc = db.query(DiscoveredVendor).filter(DiscoveredVendor.uen == body.uen).first()
            if disc:
                result.update({
                    "verified":  False,
                    "company":   disc.company_name,
                    "uen":       body.uen,
                    "framework": "Not registered on Booppa",
                    "depth":     "UNVERIFIED",
                    "error":     "Vendor found in ACRA/GeBIZ but has not completed Booppa verification.",
                })
            else:
                result["error"] = "UEN not found in Booppa registry or ACRA data."

    # ── Look up by TX hash via CertificateLog ─────────────────────────────────
    elif body.hash:
        try:
            from app.core.models_v10 import CertificateLog
            log = db.query(CertificateLog).filter(
                CertificateLog.tx_hash == body.hash  # type: ignore[union-attr]
            ).first()
            if log:
                user = db.query(User).filter(User.id == log.vendor_id).first()
                score    = db.query(VendorScore).filter(VendorScore.vendor_id == log.vendor_id).first()
                snapshot = db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id == log.vendor_id).first()
                result.update({
                    "verified":   True,
                    "company":    user.company if user else "Unknown",
                    "uen":        getattr(user, "uen", None) if user else None,
                    "framework":  "Vendor Proof v2",
                    "trust_score": score.total_score if score else None,
                    "depth":      snapshot.verification_depth if snapshot else "UNVERIFIED",
                    "tx_hash":    body.hash,
                    "anchored_at": log.created_at.strftime("%-d %b %Y, %H:%M SGT") if log.created_at else None,
                })
            else:
                result["error"] = "Transaction hash not found in Booppa records."
        except Exception as e:
            logger.warning(f"CertificateLog lookup failed: {e}")
            result["error"] = "Hash lookup temporarily unavailable."

    return result


# ── POST /shortlist-report ────────────────────────────────────────────────────

class ShortlistReportRequest(BaseModel):
    vendors:      list[dict]
    officer:      Optional[str] = None
    tender_ref:   Optional[str] = None


@router.post("/shortlist-report", response_class=PlainTextResponse)
def generate_shortlist_report(body: ShortlistReportRequest, db: Session = Depends(get_db)):
    """
    Generate a plain-text AGO-auditable evaluation shortlist.
    Looks up real blockchain TX hashes from CertificateLog per vendor.
    """
    import hashlib, json

    # Pre-fetch TX hashes for all vendor UENs
    tx_map: dict[str, str] = {}
    try:
        from app.core.models_v10 import CertificateLog
        uens = [v.get("uen") for v in body.vendors if v.get("uen")]
        if uens:
            users = db.query(User).filter(User.uen.in_(uens)).all()  # type: ignore[union-attr]
            uid_to_uen = {str(u.id): u.uen for u in users}
            logs = (
                db.query(CertificateLog)
                .filter(CertificateLog.vendor_id.in_([u.id for u in users]))
                .order_by(CertificateLog.created_at.desc())
                .all()
            )
            seen: set[str] = set()
            for log in logs:
                uid = str(log.vendor_id)
                if uid not in seen and uid in uid_to_uen:
                    uen = uid_to_uen[uid]
                    tx = getattr(log, "tx_hash", None)
                    if tx:
                        tx_map[uen] = tx
                    seen.add(uid)
    except Exception as e:
        logger.warning(f"CertificateLog lookup for shortlist failed: {e}")

    now_date = datetime.now(timezone.utc).strftime("%-d %B %Y")
    now_time = datetime.now(timezone.utc).strftime("%H:%M UTC")
    sep = "─" * 60

    lines = [
        "BOOPPA PROCUREMENT INTELLIGENCE",
        "Vendor Evaluation Shortlist",
        f"Generated:           {now_date} at {now_time}",
        f"Procurement Officer: {body.officer or 'Not specified'}",
        f"Tender Reference:    {body.tender_ref or 'Not specified'}",
        f"Framework:           Booppa Automated Compliance Framework v1.0",
        f"Assessing Entity:    Booppa Smart Care LLC  ·  UEN 202415732W",
        sep,
        f"VENDORS EVALUATED ({len(body.vendors)})",
        "",
    ]

    for i, v in enumerate(body.vendors, 1):
        uen   = v.get("uen") or "—"
        score = v.get("trust_score", 0)
        depth = v.get("depth", "UNVERIFIED")
        risk  = v.get("risk", "CLEAN")
        ready = v.get("readiness", "NEEDS_ATTENTION")
        pct   = v.get("percentile")
        vrf   = v.get("verified", False)
        tx    = tx_map.get(uen)
        anchor = tx if tx else ("Not anchored" if not vrf else "Anchored — TX hash unavailable")

        lines += [
            f"{i}. {v.get('name', 'Unknown')}",
            f"   UEN (Singapore Business Registration No.): {uen}",
            f"   Trust Score:              {score}/100",
            f"   Verification Depth:       {depth}",
            f"   Risk Signal:              {risk}",
            f"   Procurement Readiness:    {ready}",
            f"   Sector Percentile:        {f'{pct}th' if pct is not None else '—'}",
            f"   Booppa Verified:          {'Yes' if vrf else 'No'}",
            f"   Blockchain Anchor:        {anchor}",
            "",
        ]

    # Document hash for AGO audit trail
    payload   = json.dumps({"vendors": body.vendors, "tender_ref": body.tender_ref, "officer": body.officer}, sort_keys=True)
    doc_hash  = hashlib.sha256(payload.encode()).hexdigest()

    lines += [
        sep,
        f"Document Hash (SHA-256): {doc_hash}",
        f"Hash Algorithm:          SHA-256",
        "",
        "This document was generated by Booppa Procurement Intelligence.",
        "It is intended for internal procurement due diligence reference.",
        "For verification: booppa.io/verify",
        "",
        "Scope of Assessment: This evaluation is based on publicly accessible vendor",
        "information, ACRA registration data, GeBIZ supplier history, and Booppa trust",
        "signals at the time of generation. It does not substitute for legal due diligence.",
        "",
        "Booppa Smart Care LLC  ·  booppa.io  ·  compliance@booppa.io  ·  UEN 202415732W",
    ]

    return "\n".join(lines)
