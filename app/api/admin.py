from fastapi import APIRouter, Request, HTTPException, Query, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.db import SessionLocal
from app.core.models import ConsentLog, EnterpriseProfile, ActivityLog, VendorScore
from app.core.config import settings
import logging
import secrets

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()


def _admin_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
):
    """Allow either X-Admin-Token header or HTTP Basic credentials (ADMIN_USER/ADMIN_PASSWORD)."""
    # Check header token first
    header = request.headers.get("x-admin-token")
    if settings.ADMIN_TOKEN:
        if header and secrets.compare_digest(header, settings.ADMIN_TOKEN):
            return True

    # Fallback to HTTP Basic if configured
    if settings.ADMIN_USER and settings.ADMIN_PASSWORD:
        if credentials:
            valid_user = secrets.compare_digest(
                credentials.username, settings.ADMIN_USER
            )
            valid_pass = secrets.compare_digest(
                credentials.password, settings.ADMIN_PASSWORD
            )
            if valid_user and valid_pass:
                return True

    # Nothing matched
    logger.warning("Admin authentication failed")
    raise HTTPException(status_code=401, detail="Unauthorized")


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
