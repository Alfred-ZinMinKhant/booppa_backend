"""
SEO Engine Service
==================
Phase 2 feature: programmatic SEO page data generation.
Generates structured data for industry pages, vendor listings, and top vendor rankings.
"""

import logging
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.models import User
from app.core.models_v6 import VendorScore, VendorSector, VerifyRecord
from app.core.models_v8 import VendorStatusSnapshot
from app.core.models_v10 import MarketplaceVendor

logger = logging.getLogger(__name__)


def get_industry_page_data(db: Session, industry_slug: str) -> Optional[dict]:
    """Generate data for an industry-specific SEO page."""
    from app.services.marketplace import generate_slug

    # Find marketplace vendors in this industry
    vendors = (
        db.query(MarketplaceVendor)
        .filter(MarketplaceVendor.industry.isnot(None))
        .all()
    )

    # Match by slug
    matched_industry = None
    industry_vendors = []
    for v in vendors:
        if generate_slug(v.industry) == industry_slug:
            matched_industry = v.industry
            industry_vendors.append(v)

    if not matched_industry:
        return None

    return {
        "industry": matched_industry,
        "slug": industry_slug,
        "vendor_count": len(industry_vendors),
        "vendors": [
            {
                "company_name": v.company_name,
                "slug": v.slug,
                "domain": v.domain,
                "short_description": v.short_description,
                "country": v.country,
                "city": v.city,
            }
            for v in industry_vendors[:50]  # Limit for page performance
        ],
        "meta": {
            "title": f"Top {matched_industry} Vendors in Singapore | Booppa",
            "description": f"Discover {len(industry_vendors)} verified {matched_industry} vendors in Singapore. Compare compliance scores, certifications, and procurement readiness.",
        },
    }


def get_top_vendors_by_sector(db: Session, sector: str, limit: int = 20) -> dict:
    """Get top-ranked vendors in a sector."""
    results = (
        db.query(VendorScore, User)
        .join(User, VendorScore.vendor_id == User.id)
        .join(VendorSector, VendorScore.vendor_id == VendorSector.vendor_id)
        .filter(VendorSector.sector.ilike(f"%{sector}%"))
        .order_by(VendorScore.total_score.desc())
        .limit(limit)
        .all()
    )

    vendors = []
    for rank, (score, user) in enumerate(results, 1):
        status = db.query(VendorStatusSnapshot).filter(
            VendorStatusSnapshot.vendor_id == user.id
        ).first()
        vendors.append({
            "rank": rank,
            "company": user.company or user.full_name,
            "total_score": score.total_score,
            "verification_depth": status.verification_depth if status else "UNVERIFIED",
            "procurement_readiness": status.procurement_readiness if status else "NOT_READY",
        })

    return {
        "sector": sector,
        "vendor_count": len(vendors),
        "vendors": vendors,
        "meta": {
            "title": f"Top {sector} Vendors | Booppa Rankings",
            "description": f"Top {len(vendors)} {sector} vendors ranked by compliance score, verification depth, and procurement readiness.",
        },
    }


def get_country_vendors(db: Session, country: str, page: int = 1, per_page: int = 20) -> dict:
    """Get vendors by country for country-specific SEO pages."""
    q = db.query(MarketplaceVendor).filter(
        MarketplaceVendor.country.ilike(f"%{country}%")
    )
    total = q.count()
    vendors = q.order_by(MarketplaceVendor.company_name).offset((page - 1) * per_page).limit(per_page).all()

    return {
        "country": country,
        "total": total,
        "page": page,
        "vendors": [
            {
                "company_name": v.company_name,
                "slug": v.slug,
                "industry": v.industry,
                "short_description": v.short_description,
            }
            for v in vendors
        ],
        "meta": {
            "title": f"Verified Vendors in {country} | Booppa",
            "description": f"Browse {total} verified vendors in {country} on Booppa marketplace.",
        },
    }


def generate_sitemap_data(db: Session) -> list[dict]:
    """Generate sitemap entries for all SEO pages."""
    entries = []

    # Industry pages
    industries = (
        db.query(MarketplaceVendor.industry, func.count(MarketplaceVendor.id))
        .filter(MarketplaceVendor.industry.isnot(None))
        .group_by(MarketplaceVendor.industry)
        .having(func.count(MarketplaceVendor.id) >= 3)
        .all()
    )
    from app.services.marketplace import generate_slug
    for industry, count in industries:
        entries.append({
            "url": f"/vendors/{generate_slug(industry)}",
            "priority": 0.8,
            "changefreq": "weekly",
        })

    # Vendor profile pages
    vendors = db.query(MarketplaceVendor.slug).limit(5000).all()
    for (slug,) in vendors:
        entries.append({
            "url": f"/entity/{slug}",
            "priority": 0.6,
            "changefreq": "monthly",
        })

    return entries
