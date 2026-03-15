"""
SEO API Routes
==============
Phase 2: Programmatic SEO pages and sitemap generation.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.feature_flags import require_feature
from app.services.seo_engine import (
    get_industry_page_data, get_top_vendors_by_sector,
    get_country_vendors, generate_sitemap_data,
)

router = APIRouter()


@router.get("/industry/{industry_slug}")
async def industry_page(
    industry_slug: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_SEO")),
):
    """Get data for an industry SEO page."""
    data = get_industry_page_data(db, industry_slug)
    if not data:
        raise HTTPException(status_code=404, detail="Industry not found")
    return data


@router.get("/top/{sector}")
async def top_vendors(
    sector: str,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_SEO")),
):
    """Get top vendors in a sector."""
    return get_top_vendors_by_sector(db, sector, limit=limit)


@router.get("/country/{country}/vendors")
async def country_vendors(
    country: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: None = Depends(require_feature("FEATURE_SEO")),
):
    """Get vendors by country."""
    return get_country_vendors(db, country, page=page, per_page=per_page)


@router.get("/sitemap")
async def sitemap(db: Session = Depends(get_db)):
    """Generate sitemap data for SEO."""
    return generate_sitemap_data(db)
