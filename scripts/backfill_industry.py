#!/usr/bin/env python3
"""
backfill_industry.py — Infer and backfill industry for MarketplaceVendor + DiscoveredVendor
============================================================================================
Uses keyword matching on company_name + short_description to assign industry
to vendors that currently have no industry set.

Usage:
    python scripts/backfill_industry.py             # apply to DB
    python scripts/backfill_industry.py --dry-run   # preview counts only
"""

import argparse
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User  # noqa: F401 — must be imported before models that FK-reference users
from app.core.models_v10 import MarketplaceVendor, DiscoveredVendor

SECTOR_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Aerospace & Defence",                ["aerospace", "aviation", "aircraft", "defence", "defense", "military", "satellite", "unmanned"]),
    ("Construction & Engineering",         ["construction", "contractor", "building", "civil engineering", "architecture", "structural", "renovation", "plumbing", "electrical installation"]),
    ("Consulting & Professional Services", ["consulting", "advisory", "audit", "accounting", "management consult", "professional", "legal", "law firm", "notary", "recruitment", "staffing", "human resource", "manpower", "payroll", "talent"]),
    ("Education & Training",               ["education", "training", "academy", "learning", "tuition", "school", "institute", "university", "enrichment"]),
    ("Energy & Utilities",                 ["energy", "power", "solar", "electricity", "gas supply", "utilities", "renewable", "petroleum", "oil and gas"]),
    ("Financial Services",                 ["financial", "fintech", "insurance", "investment", "fund", "capital", "banking", "wealth", "credit", "securities"]),
    ("Food & Beverage",                    ["food", "beverage", "restaurant", "catering", "bakery", "cafe", "cuisine", "coffee", "canteen"]),
    ("Healthcare & Pharmaceuticals",       ["health", "medical", "clinic", "hospital", "pharmacy", "biotech", "dental", "therapeutic", "pharmaceutical", "nursing"]),
    ("Information Technology",             ["software", "technology", "digital", "ict", "cloud", "data", "ai ", "saas", "infocomm", "app development", "cybersecurity", "cyber security", "infosec", "it solutions", "it services"]),
    ("Logistics & Transportation",         ["logistics", "transport", "freight", "courier", "shipping", "warehousing", "forwarding", "cargo", "delivery", "moving"]),
    ("Manufacturing",                      ["engineering", "manufacturing", "industrial", "fabrication", "precision", "machining", "assembly", "production"]),
    ("Marine & Offshore",                  ["marine", "offshore", "shipyard", "ship", "vessel", "maritime", "port", "diving"]),
    ("Media & Communications",             ["marketing", "media", "advertising", "public relations", "design", "creative", "branding", "publishing", "broadcasting", "film"]),
    ("Real Estate & Property",             ["property", "real estate", "realty", "property development", "facilities management", "strata"]),
    ("Retail & E-Commerce",               ["retail", "e-commerce", "ecommerce", "shop", "store", "trading", "wholesale", "merchandise", "supermarket"]),
    ("Telecommunications",                 ["telecom", "telecommunication", "network", "broadband", "wireless", "mobile operator", "fibre"]),
]


def infer_industry(text: str) -> str:
    t = text.lower()
    for industry, kws in SECTOR_KEYWORDS:
        if any(kw in t for kw in kws):
            return industry
    return "Other"


def run(dry_run: bool) -> None:
    db = SessionLocal()
    stats = Counter()

    try:
        # Backfill MarketplaceVendor
        mv_query = db.query(MarketplaceVendor).filter(
            (MarketplaceVendor.industry == None) | (MarketplaceVendor.industry == "")
        )
        mv_count = mv_query.count()
        print(f"[backfill] MarketplaceVendor without industry: {mv_count}")

        for mv in mv_query.all():
            text = f"{mv.company_name} {mv.short_description or ''}"
            industry = infer_industry(text)
            stats[industry] += 1
            if not dry_run:
                mv.industry = industry

        if not dry_run and mv_count > 0:
            db.commit()
            print(f"[backfill] Updated {mv_count} MarketplaceVendor records")

        # Backfill DiscoveredVendor
        dv_query = db.query(DiscoveredVendor).filter(
            (DiscoveredVendor.industry == None) | (DiscoveredVendor.industry == "")
        )
        dv_count = dv_query.count()
        print(f"[backfill] DiscoveredVendor without industry: {dv_count}")

        for dv in dv_query.all():
            text = f"{dv.company_name}"
            industry = infer_industry(text)
            stats[industry] += 1
            if not dry_run:
                dv.industry = industry

        if not dry_run and dv_count > 0:
            db.commit()
            print(f"[backfill] Updated {dv_count} DiscoveredVendor records")

    finally:
        db.close()

    print(f"\n[backfill] Industry distribution ({'' if dry_run else 'applied'}{' (dry run)' if dry_run else ''}):")
    for industry, count in stats.most_common():
        print(f"  {industry:<40} {count:>6}")
    print(f"  {'TOTAL':<40} {sum(stats.values()):>6}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill industry for vendors without one")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()
    run(args.dry_run)
