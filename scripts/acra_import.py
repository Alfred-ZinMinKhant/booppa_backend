#!/usr/bin/env python3
"""
acra_import.py — ACRA Dataset Importer for Booppa
===================================================
Downloads the ACRA dataset from data.gov.sg, filters by entity type,
deduplicates on UEN, and produces a CSV ready for import via
POST /api/marketplace/import/csv or seed_vendors.py.

Usage:
    python scripts/acra_import.py                        # download + output CSV
    python scripts/acra_import.py --dry-run              # counts without writing
    python scripts/acra_import.py --limit 5000           # limit rows (dev)
    python scripts/acra_import.py --out data/acra.csv
    python scripts/acra_import.py --dataset <ID>         # override dataset ID

Dataset IDs (data.gov.sg):
    d_82ce0e3a0ce059e0a7b36c43e4cd5c96    (current d_ format)
    5ab68aac-91f6-4f39-9b21-698610bdf3f7  (legacy CKAN UUID)

Dependencies:
    pip install requests
"""

import argparse
import csv
import io
import sys
import time
from collections import Counter
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: pip install requests", file=sys.stderr)
    sys.exit(1)

# ─── Config ───────────────────────────────────────────────────────────────────

API_BASE = "https://data.gov.sg/api/action/datastore_search"

KNOWN_DATASET_IDS = [
    "d_82ce0e3a0ce059e0a7b36c43e4cd5c96",
    "5ab68aac-91f6-4f39-9b21-698610bdf3f7",
]

ACCEPTED_TYPES = {
    "PRIVATE COMPANY LIMITED BY SHARES",
    "PUBLIC COMPANY LIMITED BY SHARES",
    "SOLE-PROPRIETORSHIP",
    "BUSINESS",
    "PARTNERSHIP",
    "LIMITED PARTNERSHIP",
    "LIMITED LIABILITY PARTNERSHIP",
}

SECTOR_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Cybersecurity",              ["cybersecurity", "cyber security", "infosec", "firewall", "pentest", "penetration test"]),
    ("Financial Services",         ["financial", "fintech", "insurance", "investment", "fund", "capital", "banking", "wealth"]),
    ("Health Technology",          ["health", "medical", "clinic", "hospital", "pharmacy", "biotech", "dental", "therapeutic"]),
    ("Logistics & Supply Chain",   ["logistics", "transport", "freight", "courier", "shipping", "warehousing", "forwarding"]),
    ("Manufacturing & Industrial", ["engineering", "manufacturing", "industrial", "construction", "fabrication", "contractor"]),
    ("HR & People Tech",           ["recruitment", "staffing", "human resource", "manpower", "payroll", "talent"]),
    ("Marketing Technology",       ["marketing", "media", "advertising", "public relations", "design", "creative", "branding"]),
    ("Education & Training",       ["education", "training", "academy", "learning", "tuition", "school", "institute"]),
    ("Food & Beverage",            ["food", "beverage", "restaurant", "catering", "bakery", "cafe", "cuisine"]),
    ("Real Estate",                ["property", "real estate", "realty", "property development", "facilities management"]),
    ("IT & Technology",            ["software", "technology", "digital", "ict", "cloud", "data", "ai ", "saas", "infocomm", "app development"]),
    ("Professional Services",      ["consulting", "advisory", "audit", "accounting", "management", "professional", "legal", "law"]),
]

PAGE_SIZE = 100
RATE_LIMIT_SEC = 0.15


def infer_sector(text: str) -> str:
    t = text.lower()
    for sector, kws in SECTOR_KEYWORDS:
        if any(kw in t for kw in kws):
            return sector
    return "Professional Services"


def fetch_page(dataset_id: str, offset: int, limit: int = PAGE_SIZE) -> dict[str, Any]:
    url = f"{API_BASE}?resource_id={dataset_id}&limit={limit}&offset={offset}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def discover_working_dataset(dataset_ids: list[str]) -> str | None:
    for did in dataset_ids:
        try:
            data = fetch_page(did, offset=0, limit=1)
            if data.get("success") and data.get("result", {}).get("records"):
                print(f"[acra_import] Active dataset ID: {did}")
                return did
        except requests.HTTPError as e:
            print(f"[acra_import] Dataset {did[:20]}… → HTTP {e.response.status_code}")
        except Exception as e:
            print(f"[acra_import] Dataset {did[:20]}… → {e}")
    return None


def normalize_row(rec: dict[str, Any]) -> dict[str, str] | None:
    entity_type = str(rec.get("entity_type", "") or "").strip().upper()
    if entity_type not in ACCEPTED_TYPES:
        return None

    uen = str(rec.get("uen", "") or "").strip()
    name = str(rec.get("entity_name", "") or "").strip()
    status = str(rec.get("entity_status", "") or "").strip().upper()

    if not uen or not name:
        return None

    if status and status not in ("LIVE", "REGISTERED", "ACTIVE"):
        return None

    primary_activity = str(rec.get("primary_ssic_description", "") or "").strip()

    return {
        "companyName":      name,
        "domain":           "",
        "website":          "",
        "industry":         infer_sector(f"{name} {primary_activity}"),
        "country":          "Singapore",
        "city":             "Singapore",
        "shortDescription": primary_activity[:255],
        "uen":              uen,
        "entityType":       entity_type.title(),
        "registrationDate": "",
    }


FIELDNAMES = [
    "companyName", "domain", "website", "industry", "country", "city",
    "shortDescription", "uen", "entityType", "registrationDate",
]


def run(dataset_id: str, output_path: str, dry_run: bool, limit: int | None) -> None:
    seen_uens: set[str] = set()
    rows: list[dict[str, str]] = []
    total_fetched = 0
    total_skipped = 0
    offset = 0

    print(f"[acra_import] Dataset: {dataset_id}")
    print(f"[acra_import] Fetching from data.gov.sg…")

    while True:
        try:
            data = fetch_page(dataset_id, offset)
        except requests.HTTPError as e:
            print(f"\n[acra_import] HTTP {e.response.status_code} at offset {offset}: {e}")
            if e.response.status_code == 404:
                print("[acra_import] ERROR: Dataset ID not found.")
                print("[acra_import] Go to https://data.gov.sg and search 'acra business entities'")
                print("[acra_import] then re-run with: --dataset <CORRECT_ID>")
            break
        except requests.ConnectionError:
            print(f"\n[acra_import] Connection failed at offset {offset}.")
            break
        except Exception as e:
            print(f"\n[acra_import] Unexpected error at offset {offset}: {e}")
            break

        records: list[dict] = data.get("result", {}).get("records", [])
        if not records:
            break

        for rec in records:
            total_fetched += 1
            row = normalize_row(rec)
            if row is None:
                total_skipped += 1
                continue
            uen = row["uen"]
            if uen in seen_uens:
                total_skipped += 1
                continue
            seen_uens.add(uen)
            rows.append(row)

        print(f"  offset={offset:>7}  accepted={len(rows):>6}  skipped={total_skipped:>6}", end="\r")

        offset += PAGE_SIZE

        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break

        time.sleep(RATE_LIMIT_SEC)

    print(f"\n[acra_import] Fetch complete: {len(rows)} accepted, {total_skipped} skipped of {total_fetched} total")

    if not rows:
        print("[acra_import] WARNING: no records produced. Check the dataset ID.")
        return

    if dry_run:
        print("[acra_import] DRY RUN — no file written")
        by_sector = Counter(r["industry"] for r in rows)
        by_type = Counter(r["entityType"] for r in rows)
        print("\nSector distribution:")
        for sector, count in by_sector.most_common(10):
            print(f"  {sector:<35} {count:>6}")
        print("\nEntity type distribution:")
        for et, count in by_type.most_common():
            print(f"  {et:<45} {count:>6}")
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    writer.writerows(rows)

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())

    print(f"[acra_import] Written: {output_path}  ({len(rows)} rows)")
    print()
    print("[acra_import] Next steps:")
    print(f"  # Import directly to DB:")
    print(f"  python scripts/seed_vendors.py --file {output_path}")
    print()
    print(f"  # Or via API (requires running server):")
    print(f"  curl -X POST http://localhost:8000/api/v1/marketplace/import/csv \\")
    print(f"       -H 'Authorization: Bearer $ADMIN_TOKEN' \\")
    print(f"       --data-binary @{output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ACRA data.gov.sg → Booppa MarketplaceVendor CSV importer"
    )
    parser.add_argument("--out", default="data/acra-import.csv",
                        help="Output CSV path (default: data/acra-import.csv)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count and show distribution without writing file")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit output to N rows (useful for testing)")
    parser.add_argument("--dataset", default=None,
                        help="Dataset ID from data.gov.sg (auto-discover if omitted)")
    args = parser.parse_args()

    if args.dataset:
        dataset_id = args.dataset
        print(f"[acra_import] Using specified dataset ID: {dataset_id}")
    else:
        print("[acra_import] Auto-discovering dataset ID…")
        dataset_id = discover_working_dataset(KNOWN_DATASET_IDS)
        if not dataset_id:
            print("[acra_import] ERROR: no working dataset ID found.")
            print("[acra_import] Specify manually with: --dataset <ID>")
            print("[acra_import] Find the ID at: https://data.gov.sg (search 'acra business entities')")
            sys.exit(1)

    run(dataset_id, args.out, args.dry_run, args.limit)
