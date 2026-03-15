"""
Discovery API Routes
====================
Vendor discovery (GeBIZ, ACRA) and claim flow.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models_v10 import DiscoveredVendor, MarketplaceVendor

router = APIRouter()


@router.get("/search")
async def search_discovered(
    q: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Search discovered vendors."""
    from sqlalchemy import or_

    query = db.query(DiscoveredVendor)

    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                DiscoveredVendor.company_name.ilike(search),
                DiscoveredVendor.uen.ilike(search),
            )
        )

    if source:
        query = query.filter(DiscoveredVendor.source == source)

    total = query.count()
    vendors = query.order_by(DiscoveredVendor.company_name).offset((page - 1) * per_page).limit(per_page).all()

    return {
        "vendors": [
            {
                "id": str(v.id),
                "company_name": v.company_name,
                "uen": v.uen,
                "domain": v.domain,
                "industry": v.industry,
                "source": v.source,
                "gebiz_supplier": v.gebiz_supplier,
                "gebiz_contracts_count": v.gebiz_contracts_count,
                "claimed": v.claimed_by_user_id is not None,
            }
            for v in vendors
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


class ClaimRequest(BaseModel):
    vendor_id: str
    user_id: str


@router.post("/claim")
async def claim_vendor(body: ClaimRequest, db: Session = Depends(get_db)):
    """Claim a discovered vendor profile."""
    from datetime import datetime

    vendor = db.query(DiscoveredVendor).filter(DiscoveredVendor.id == body.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if vendor.claimed_by_user_id:
        raise HTTPException(status_code=409, detail="Vendor already claimed")

    vendor.claimed_by_user_id = body.user_id
    db.commit()

    # Also check marketplace vendors
    if vendor.uen:
        mv = db.query(MarketplaceVendor).filter(MarketplaceVendor.uen == vendor.uen).first()
        if mv and not mv.claimed_by_user_id:
            mv.claimed_by_user_id = body.user_id
            mv.claimed_at = datetime.utcnow()
            db.commit()

    return {"message": "Vendor claimed successfully", "vendor_id": str(vendor.id)}
