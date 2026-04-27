"""
Marketplace API Routes
======================
Vendor directory, CSV import, search, and entity profiles.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from app.core.db import get_db
from app.services.marketplace import (
    import_csv_data, search_marketplace, get_vendor_by_slug, get_industries, get_trust_status,
)

router = APIRouter()


@router.get("/stats")
def marketplace_stats(db: Session = Depends(get_db)):
    """Public platform stats for government landing page."""
    from sqlalchemy import func
    from app.core.models_v10 import MarketplaceVendor
    from app.core.models_v6 import VerifyRecord, LifecycleStatus
    from app.core.models_gebiz import GebizTender
    from datetime import datetime, timezone

    total_vendors = db.query(func.count(MarketplaceVendor.id)).scalar() or 0
    verified_vendors = (
        db.query(func.count(VerifyRecord.id))
        .filter(VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE)
        .scalar()
        or 0
    )
    now = datetime.now(timezone.utc)
    active_tenders = (
        db.query(func.count(GebizTender.id))
        .filter(
            GebizTender.status == "Open",
            (GebizTender.closing_date == None) | (GebizTender.closing_date >= now),
        )
        .scalar()
        or 0
    )

    return {
        "total_vendors": total_vendors,
        "verified_vendors": verified_vendors,
        "active_tenders": active_tenders,
    }


class ImportResponse(BaseModel):
    batch_id: Optional[str] = None
    total_rows: int
    imported: int
    skipped: int
    errors: int
    dry_run: bool


@router.get("/search")
async def search_vendors(
    q: Optional[str] = Query(None, description="Search query"),
    industry: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    verified: Optional[bool] = Query(None, description="Filter to verified vendors only"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Search marketplace vendors."""
    return search_marketplace(db, query=q, industry=industry, country=country, verified=verified, page=page, per_page=per_page)


@router.get("/industries")
async def list_industries(db: Session = Depends(get_db)):
    """List all industries with vendor counts."""
    return get_industries(db)


@router.get("/trust-status")
async def trust_status(
    q: str = Query(..., description="Company name to look up"),
    db: Session = Depends(get_db),
):
    """
    Public trust status lookup by company name.
    Returns verified/not-verified status for the /check-status page.
    No authentication required.
    """
    result = get_trust_status(db, q)
    if not result:
        raise HTTPException(status_code=404, detail="Company not found in BOOPPA network")
    return result


@router.get("/vendor/{slug}")
async def get_vendor(slug: str, db: Session = Depends(get_db)):
    """Get vendor by slug."""
    vendor = get_vendor_by_slug(db, slug)
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.post("/import/csv", response_model=ImportResponse)
async def import_csv(
    file: UploadFile = File(...),
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV file."""
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()
    try:
        csv_text = content.decode("utf-8")
    except UnicodeDecodeError:
        csv_text = content.decode("latin-1")

    result = import_csv_data(db, csv_text, filename=file.filename, dry_run=dry_run)
    return result
