#!/usr/bin/env python3
"""
Standalone industry backfill — no app imports required.
Run on any machine that can reach the database.

Usage:
    DATABASE_URL="postgresql://user:pass@host:5432/db" python3 backfill_industry_standalone.py
    DATABASE_URL="postgresql://user:pass@host:5432/db" python3 backfill_industry_standalone.py --dry-run

Install deps if needed:
    pip3 install psycopg2-binary
"""

import argparse
import os
import sys
from collections import Counter

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("psycopg2 not found. Run: pip3 install psycopg2-binary")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set.")
    sys.exit(1)

# Strip SQLAlchemy dialect prefix if present
if DATABASE_URL.startswith("postgresql+psycopg2://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://", 1)

SECTOR_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "Aerospace & Defence",
        [
            "aerospace",
            "aviation",
            "aircraft",
            "defence",
            "defense",
            "military",
            "satellite",
            "unmanned",
        ],
    ),
    (
        "Construction & Engineering",
        [
            "construction",
            "contractor",
            "building",
            "civil engineering",
            "architecture",
            "structural",
            "renovation",
            "plumbing",
            "electrical installation",
        ],
    ),
    (
        "Consulting & Professional Services",
        [
            "consulting",
            "advisory",
            "audit",
            "accounting",
            "management consult",
            "professional",
            "legal",
            "law firm",
            "notary",
            "recruitment",
            "staffing",
            "human resource",
            "manpower",
            "payroll",
            "talent",
        ],
    ),
    (
        "Education & Training",
        [
            "education",
            "training",
            "academy",
            "learning",
            "tuition",
            "school",
            "institute",
            "university",
            "enrichment",
        ],
    ),
    (
        "Energy & Utilities",
        [
            "energy",
            "power",
            "solar",
            "electricity",
            "gas supply",
            "utilities",
            "renewable",
            "petroleum",
            "oil and gas",
        ],
    ),
    (
        "Financial Services",
        [
            "financial",
            "fintech",
            "insurance",
            "investment",
            "fund",
            "capital",
            "banking",
            "wealth",
            "credit",
            "securities",
        ],
    ),
    (
        "Food & Beverage",
        [
            "food",
            "beverage",
            "restaurant",
            "catering",
            "bakery",
            "cafe",
            "cuisine",
            "coffee",
            "canteen",
        ],
    ),
    (
        "Healthcare & Pharmaceuticals",
        [
            "health",
            "medical",
            "clinic",
            "hospital",
            "pharmacy",
            "biotech",
            "dental",
            "therapeutic",
            "pharmaceutical",
            "nursing",
        ],
    ),
    (
        "Information Technology",
        [
            "software",
            "technology",
            "digital",
            "ict",
            "cloud",
            "data",
            "ai ",
            "saas",
            "infocomm",
            "app development",
            "cybersecurity",
            "cyber security",
            "infosec",
            "it solutions",
            "it services",
        ],
    ),
    (
        "Logistics & Transportation",
        [
            "logistics",
            "transport",
            "freight",
            "courier",
            "shipping",
            "warehousing",
            "forwarding",
            "cargo",
            "delivery",
            "moving",
        ],
    ),
    (
        "Manufacturing",
        [
            "engineering",
            "manufacturing",
            "industrial",
            "fabrication",
            "precision",
            "machining",
            "assembly",
            "production",
        ],
    ),
    (
        "Marine & Offshore",
        [
            "marine",
            "offshore",
            "shipyard",
            "ship",
            "vessel",
            "maritime",
            "port",
            "diving",
        ],
    ),
    (
        "Media & Communications",
        [
            "marketing",
            "media",
            "advertising",
            "public relations",
            "design",
            "creative",
            "branding",
            "publishing",
            "broadcasting",
            "film",
        ],
    ),
    (
        "Real Estate & Property",
        [
            "property",
            "real estate",
            "realty",
            "property development",
            "facilities management",
            "strata",
        ],
    ),
    (
        "Retail & E-Commerce",
        [
            "retail",
            "e-commerce",
            "ecommerce",
            "shop",
            "store",
            "trading",
            "wholesale",
            "merchandise",
            "supermarket",
        ],
    ),
    (
        "Telecommunications",
        [
            "telecom",
            "telecommunication",
            "network",
            "broadband",
            "wireless",
            "mobile operator",
            "fibre",
        ],
    ),
]


def infer_industry(text: str) -> str:
    t = text.lower()
    for industry, kws in SECTOR_KEYWORDS:
        if any(kw in t for kw in kws):
            return industry
    return "Other"


def run(dry_run: bool) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    stats: Counter = Counter()

    try:
        # --- MarketplaceVendor ---
        cur.execute(
            """
            SELECT id, company_name, short_description
            FROM marketplace_vendors
            WHERE industry IS NULL OR industry = ''
        """
        )
        mv_rows = cur.fetchall()
        print(f"[backfill] marketplace_vendors without industry: {len(mv_rows)}")

        updates_mv = []
        for row in mv_rows:
            text = f"{row['company_name']} {row['short_description'] or ''}"
            industry = infer_industry(text)
            stats[industry] += 1
            updates_mv.append((industry, row["id"]))

        if not dry_run and updates_mv:
            cur.executemany(
                "UPDATE marketplace_vendors SET industry = %s WHERE id = %s", updates_mv
            )
            conn.commit()
            print(f"[backfill] Updated {len(updates_mv)} marketplace_vendors records")

        # --- DiscoveredVendor ---
        cur.execute(
            """
            SELECT id, company_name
            FROM discovered_vendors
            WHERE industry IS NULL OR industry = ''
        """
        )
        dv_rows = cur.fetchall()
        print(f"[backfill] discovered_vendors without industry: {len(dv_rows)}")

        updates_dv = []
        for row in dv_rows:
            industry = infer_industry(row["company_name"])
            stats[industry] += 1
            updates_dv.append((industry, row["id"]))

        if not dry_run and updates_dv:
            cur.executemany(
                "UPDATE discovered_vendors SET industry = %s WHERE id = %s", updates_dv
            )
            conn.commit()
            print(f"[backfill] Updated {len(updates_dv)} discovered_vendors records")

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        cur.close()
        conn.close()

    label = "(dry run)" if dry_run else "(applied)"
    print(f"\n[backfill] Industry distribution {label}:")
    for industry, count in stats.most_common():
        print(f"  {industry:<40} {count:>6}")
    print(f"  {'TOTAL':<40} {sum(stats.values()):>6}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing to DB"
    )
    args = parser.parse_args()
    run(args.dry_run)
