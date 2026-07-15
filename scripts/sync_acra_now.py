#!/usr/bin/env python3
"""
sync_acra_now.py — Manually trigger an ACRA offline refresh
============================================================
Pulls the ACRA business-entities dataset from data.gov.sg and upserts LIVE
entities into the `discovered_vendors` table immediately, without waiting for
Celery Beat. Mirrors the monthly `refresh_acra` task.

Usage:
    python scripts/sync_acra_now.py                 # default cap (50k rows)
    python scripts/sync_acra_now.py --max 5000      # smaller dev pull
    python scripts/sync_acra_now.py --all           # no cap (full register)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.services.acra_service import refresh_acra, DEFAULT_MAX_RECORDS


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual ACRA offline refresh")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_RECORDS,
                        help=f"Max rows to upsert (default: {DEFAULT_MAX_RECORDS})")
    parser.add_argument("--all", action="store_true",
                        help="Pull the entire register (ignore --max)")
    args = parser.parse_args()

    max_records = None if args.all else args.max

    db = SessionLocal()
    try:
        count = refresh_acra(db, max_records=max_records)
        print(f"[ACRA] refresh complete — {count} DiscoveredVendor row(s) upserted.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
