"""
Marketplace Service
===================
CSV import, vendor directory search, slug generation, deduplication.
"""

import csv
import io
import re
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from app.core.models_v10 import MarketplaceVendor, ImportBatch
from app.core.models_v6 import VerifyRecord, Proof, LifecycleStatus

logger = logging.getLogger(__name__)


def generate_slug(name: str) -> str:
    """Generate URL-safe slug from company name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    slug = slug.strip("-")
    return slug[:200]


def _find_duplicate(
    db: Session, domain: Optional[str], uen: Optional[str], slug: str
) -> Optional[MarketplaceVendor]:
    """Check for existing vendor by UEN (primary), domain, or slug."""
    if uen:
        existing = (
            db.query(MarketplaceVendor).filter(MarketplaceVendor.uen == uen).first()
        )
        if existing:
            return existing
    if domain:
        existing = (
            db.query(MarketplaceVendor)
            .filter(MarketplaceVendor.domain == domain)
            .first()
        )
        if existing:
            return existing
    existing = (
        db.query(MarketplaceVendor).filter(MarketplaceVendor.slug == slug).first()
    )
    return existing


def import_csv_data(
    db: Session,
    csv_content: str,
    filename: str = "upload.csv",
    source: str = "csv",
    created_by: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Import vendors from CSV content string."""
    batch = ImportBatch(
        filename=filename,
        source=source,
        status="PROCESSING",
        started_at=datetime.now(timezone.utc),
        created_by=created_by,
    )
    if not dry_run:
        db.add(batch)
        db.flush()

    reader = csv.DictReader(io.StringIO(csv_content))
    imported = 0
    skipped = 0
    errors = []
    total = 0

    for row in reader:
        total += 1
        try:
            name = row.get("company_name", "").strip()
            if not name:
                errors.append({"row": total, "error": "Missing company_name"})
                continue

            domain = row.get("domain", "").strip() or row.get("website", "").strip()
            if domain:
                domain = re.sub(r"^https?://", "", domain).split("/")[0].lower()

            uen = row.get("uen", "").strip() or None
            slug = generate_slug(name)

            existing = _find_duplicate(db, domain, uen, slug)
            if existing:
                skipped += 1
                continue

            # Make slug unique
            base_slug = slug
            counter = 1
            while (
                db.query(MarketplaceVendor)
                .filter(MarketplaceVendor.slug == slug)
                .first()
            ):
                slug = f"{base_slug}-{counter}"
                counter += 1

            vendor = MarketplaceVendor(
                company_name=name,
                slug=slug,
                domain=domain or None,
                website=row.get("website", "").strip() or None,
                uen=uen,
                industry=row.get("industry", "").strip() or None,
                country=row.get("country", "").strip() or "Singapore",
                city=row.get("city", "").strip() or None,
                short_description=row.get("short_description", "").strip() or None,
                linkedin_url=row.get("linkedin_url", "").strip() or None,
                crunchbase_url=row.get("crunchbase_url", "").strip() or None,
                import_batch_id=batch.id if not dry_run else None,
                source=source,
            )

            if not dry_run:
                db.add(vendor)
            imported += 1

        except Exception as e:
            errors.append({"row": total, "error": str(e)})

    if not dry_run:
        batch.total_rows = total
        batch.imported_count = imported
        batch.skipped_count = skipped
        batch.error_count = len(errors)
        batch.errors = errors[:100]  # Limit stored errors
        batch.status = "COMPLETE"
        batch.completed_at = datetime.now(timezone.utc)
        db.commit()

    return {
        "batch_id": str(batch.id) if not dry_run else None,
        "total_rows": total,
        "imported": imported,
        "skipped": skipped,
        "errors": len(errors),
        "error_details": errors[:20],
        "dry_run": dry_run,
    }


def search_marketplace(
    db: Session,
    query: Optional[str] = None,
    industry: Optional[str] = None,
    country: Optional[str] = None,
    verified: Optional[bool] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """Search marketplace vendors with pagination. verified=True filters to active VerifyRecords."""
    q = db.query(MarketplaceVendor)

    if query:
        search = f"%{query}%"
        q = q.filter(
            or_(
                MarketplaceVendor.company_name.ilike(search),
                MarketplaceVendor.short_description.ilike(search),
                MarketplaceVendor.industry.ilike(search),
            )
        )

    if industry:
        q = q.filter(MarketplaceVendor.industry.ilike(f"%{industry}%"))

    if country:
        q = q.filter(MarketplaceVendor.country.ilike(f"%{country}%"))

    # verified=True: only show vendors with an ACTIVE VerifyRecord (i.e. paid Vendor Proof)
    if verified is True:
        active_user_ids = (
            db.query(VerifyRecord.vendor_id)
            .filter(VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE)
            .subquery()
        )
        q = q.filter(MarketplaceVendor.claimed_by_user_id.in_(active_user_ids))

    total = q.count()
    vendors = (
        q.order_by(MarketplaceVendor.company_name)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Bulk-fetch verified status for result set to avoid N+1
    claimed_user_ids = [v.claimed_by_user_id for v in vendors if v.claimed_by_user_id]
    verified_set: set = set()
    if claimed_user_ids:
        rows = (
            db.query(VerifyRecord.vendor_id)
            .filter(
                VerifyRecord.vendor_id.in_(claimed_user_ids),
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .all()
        )
        verified_set = {str(r.vendor_id) for r in rows}

    return {
        "vendors": [
            {
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
                "scan_status": v.scan_status,
                "claimed": v.claimed_by_user_id is not None,
                "verified": (
                    str(v.claimed_by_user_id) in verified_set
                    if v.claimed_by_user_id
                    else False
                ),
            }
            for v in vendors
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


def get_vendor_by_slug(db: Session, slug: str) -> Optional[dict]:
    """Get a single marketplace vendor by slug."""
    v = db.query(MarketplaceVendor).filter(MarketplaceVendor.slug == slug).first()
    if not v:
        return None

    claimed = v.claimed_by_user_id is not None
    is_verified = False
    verified_at = None
    if claimed:
        vr = (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.vendor_id == v.claimed_by_user_id,
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .first()
        )
        if vr:
            is_verified = True
            verified_at = (
                (v.claimed_at or vr.created_at).isoformat()
                if (v.claimed_at or vr.created_at)
                else None
            )

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
        "linkedin_url": v.linkedin_url,
        "crunchbase_url": v.crunchbase_url,
        "scan_status": v.scan_status,
        "claimed": claimed,
        "verified": is_verified,
        "verified_at": verified_at,
        "claimed_at": v.claimed_at.isoformat() if v.claimed_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def get_trust_status(db: Session, company_name: str) -> Optional[dict]:
    """
    Public trust status lookup by company name.
    Returns verified/not-verified based on claimed profile + VerifyRecord proofs.
    """
    search = f"%{company_name}%"
    vendor = (
        db.query(MarketplaceVendor)
        .filter(MarketplaceVendor.company_name.ilike(search))
        .order_by(
            # Prefer claimed vendors first, then exact-ish matches
            MarketplaceVendor.claimed_by_user_id.isnot(None).desc(),
            MarketplaceVendor.company_name,
        )
        .first()
    )

    if not vendor:
        return None

    claimed = vendor.claimed_by_user_id is not None
    evidence_count = 0
    verification_date = None

    if claimed:
        verify = (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.vendor_id == vendor.claimed_by_user_id,
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .first()
        )
        if verify:
            evidence_count = (
                db.query(Proof).filter(Proof.verify_id == verify.id).count()
            )
            verification_date = vendor.claimed_at or verify.created_at

    return {
        "company_name": vendor.company_name,
        "verified": claimed,
        "verification_date": (
            verification_date.isoformat() if verification_date else None
        ),
        "evidence_count": evidence_count,
        "profile_url": f"/vendors/{vendor.slug}",
        "slug": vendor.slug,
    }


def get_industries(db: Session) -> list[dict]:
    """Get all industries with vendor counts."""
    results = (
        db.query(
            MarketplaceVendor.industry,
            func.count(MarketplaceVendor.id).label("count"),
        )
        .filter(MarketplaceVendor.industry.isnot(None))
        .group_by(MarketplaceVendor.industry)
        .order_by(func.count(MarketplaceVendor.id).desc())
        .all()
    )
    return [
        {"industry": r[0], "count": r[1], "slug": generate_slug(r[0])} for r in results
    ]
