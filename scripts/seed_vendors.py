#!/usr/bin/env python3
"""
seed_vendors.py — Populate MarketplaceVendor / DiscoveredVendor from CSV
=========================================================================
Reads a CSV (ACRA import or singapore-companies-seed.csv) and upserts
into the database. Idempotent: deduplicates on UEN, domain, or company name.

Usage:
    python scripts/seed_vendors.py                              # default CSV
    python scripts/seed_vendors.py --file data/acra-import.csv  # custom CSV
    python scripts/seed_vendors.py --dry-run                    # counts only
    python scripts/seed_vendors.py --limit 500                  # dev limit

Prerequisites:
    - DATABASE_URL in environment or config.yml
    - Alembic migrations applied (models_v10 tables exist)
"""

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models_v10 import MarketplaceVendor, DiscoveredVendor


def generate_slug(name: str, uen: str | None = None) -> str:
    """Generate URL-safe slug from company name, optionally with UEN suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    if uen:
        uen_suffix = re.sub(r"[^a-z0-9]", "", uen.lower())[:12]
        return f"{base}-{uen_suffix}"
    return base


def make_claim_token(company_name: str, domain: str | None, line_no: int) -> str:
    key = f"{company_name}:{domain or line_no}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"seed-{digest}"


def parse_csv_row(headers: list[str], values: list[str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for i, h in enumerate(headers):
        row[h] = (values[i] if i < len(values) else "").strip()
    return row


def run(csv_path: str, dry_run: bool, limit: int | None) -> None:
    path = Path(csv_path)
    if not path.exists():
        print(f"[seed_vendors] ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    print(f"[seed_vendors] Reading: {csv_path}")
    if dry_run:
        print("[seed_vendors] DRY RUN — no DB writes")
    if limit:
        print(f"[seed_vendors] Limit: {limit} rows")

    db = SessionLocal()
    inserted = 0
    updated = 0
    skipped = 0
    errors = 0

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = None

            for line_no, values in enumerate(reader, 1):
                if line_no == 1:
                    headers = [
                        re.sub(r"\s+", "_", h.strip().lower())
                        for h in values
                    ]
                    continue

                if not headers:
                    continue

                if limit and (inserted + updated) >= limit:
                    break

                if len(values) < 2:
                    skipped += 1
                    continue

                row = parse_csv_row(headers, values)

                company_name = row.get("company_name") or row.get("companyname") or ""
                domain = row.get("domain") or None
                sector = row.get("industry") or None
                uen = row.get("uen") or None
                entity_type = row.get("entitytype") or row.get("entity_type") or None
                reg_date = row.get("registrationdate") or row.get("registration_date") or None
                short_desc = row.get("shortdescription") or row.get("short_description") or None
                country = row.get("country") or "Singapore"
                city = row.get("city") or "Singapore"

                if not company_name:
                    skipped += 1
                    continue

                if dry_run:
                    inserted += 1
                    if inserted % 500 == 0:
                        print(f"  dry-run rows: {inserted}", end="\r")
                    continue

                try:
                    if uen:
                        # ACRA data: upsert MarketplaceVendor keyed on UEN
                        existing = db.query(MarketplaceVendor).filter(
                            MarketplaceVendor.uen == uen
                        ).first()

                        if existing:
                            if entity_type:
                                existing.entity_type = entity_type
                            if sector:
                                existing.industry = sector
                            if reg_date:
                                existing.registration_date = reg_date
                            db.commit()
                            updated += 1
                        else:
                            seo_slug = generate_slug(company_name, uen)
                            vendor = MarketplaceVendor(
                                company_name=company_name,
                                seo_slug=seo_slug,
                                domain=domain,
                                industry=sector,
                                country=country,
                                city=city,
                                uen=uen,
                                entity_type=entity_type,
                                registration_date=reg_date,
                                short_description=short_desc,
                                discovery_source="CSV_IMPORT",
                            )
                            db.add(vendor)
                            db.commit()
                            inserted += 1

                    elif domain:
                        # Domain-based: upsert DiscoveredVendor
                        existing = db.query(DiscoveredVendor).filter(
                            DiscoveredVendor.domain == domain
                        ).first()

                        if existing:
                            existing.company_name = company_name
                            if sector:
                                existing.sector = sector
                            db.commit()
                            updated += 1
                        else:
                            claim_token = make_claim_token(company_name, domain, line_no)
                            discovered = DiscoveredVendor(
                                company_name=company_name,
                                domain=domain,
                                sector=sector,
                                scan_status="SCANNING",
                                claim_token=claim_token,
                            )
                            db.add(discovered)
                            db.commit()
                            inserted += 1

                    else:
                        # No UEN, no domain: fall back to name dedup
                        from sqlalchemy import func
                        existing = db.query(DiscoveredVendor).filter(
                            func.lower(DiscoveredVendor.company_name) == company_name.lower()
                        ).first()

                        if existing:
                            skipped += 1
                            continue

                        claim_token = make_claim_token(company_name, None, line_no)
                        discovered = DiscoveredVendor(
                            company_name=company_name,
                            sector=sector,
                            scan_status="SCANNING",
                            claim_token=claim_token,
                        )
                        db.add(discovered)
                        db.commit()
                        inserted += 1

                except Exception as e:
                    db.rollback()
                    if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                        skipped += 1
                        continue
                    print(f"\n[seed_vendors] Row {line_no} error: {e}")
                    errors += 1
                    if errors > 50:
                        print("[seed_vendors] Too many errors — aborting")
                        break

                if (inserted + updated) % 200 == 0 and (inserted + updated) > 0:
                    print(f"  inserted={inserted} updated={updated} skipped={skipped}", end="\r")

    finally:
        db.close()

    print(f"\n[seed_vendors] Complete:")
    print(f"  inserted : {inserted}")
    print(f"  updated  : {updated}")
    print(f"  skipped  : {skipped}")
    print(f"  errors   : {errors}")

    if not dry_run and errors == 0:
        db2 = SessionLocal()
        try:
            dv_count = db2.query(DiscoveredVendor).count()
            mv_count = db2.query(MarketplaceVendor).count()
            print(f"  DiscoveredVendor total  : {dv_count}")
            print(f"  MarketplaceVendor total : {mv_count}")
        finally:
            db2.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed MarketplaceVendor / DiscoveredVendor from CSV"
    )
    parser.add_argument("--file", default="data/singapore-companies-seed.csv",
                        help="Input CSV path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing to DB")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit to N rows")
    args = parser.parse_args()

    run(args.file, args.dry_run, args.limit)
