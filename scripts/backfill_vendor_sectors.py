#!/usr/bin/env python3
"""
backfill_vendor_sectors.py — Seed VendorSector from User.industry
==================================================================
Tender Intelligence scopes its monthly digest to the subscriber's
`VendorSector` rows. Historically that table was written in exactly one place —
Vendor Active/Pro fulfillment, from `report.assessment_data` — so an account
that subscribed to Tender Intelligence *standalone* never got a row and
received a permanent "all sectors" digest rather than a first-cycle-only one.

There is no registry fallback to lean on: the open ACRA dataset on data.gov.sg
publishes no SSIC or activity field (uen, status, entity name, entity type,
issue date, address — that's all), so a UEN cannot tell us a subscriber's
sector. `User.industry` is the authoritative source we actually have.

The runtime paths that seed VendorSector (registration, subscription
activation, profile update, digest-time fallback) are now in place, but they
only fire going forward. This backfills accounts that already have
`User.industry` set and no matching `VendorSector` row.

Accounts with no industry on file cannot be resolved here — nothing to infer
from. They are counted and listed under --show-unresolvable so they can be
prompted at their next Tender Intelligence touchpoint.

Usage:
    python scripts/backfill_vendor_sectors.py --dry-run          # preview
    python scripts/backfill_vendor_sectors.py                    # apply
    python scripts/backfill_vendor_sectors.py --show-unresolvable
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User  # noqa: F401 — import before models that FK to users
from app.core.models import VendorSector
from app.services.tender_service import sync_vendor_sector, _CATEGORY_TO_SECTOR


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    ap.add_argument("--show-unresolvable", action="store_true",
                    help="list accounts with no industry on file")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # Every user id that already has at least one sector row.
        have_sector = {r[0] for r in db.query(VendorSector.vendor_id).distinct().all()}

        users = db.query(User).all()
        seeded, skipped, unresolvable = 0, 0, []

        for u in users:
            if u.id in have_sector:
                skipped += 1
                continue
            industry = (getattr(u, "industry", "") or "").strip()
            if not industry:
                unresolvable.append(u)
                continue

            resolved = _CATEGORY_TO_SECTOR.get(industry) or industry
            if args.dry_run:
                print(f"  would seed {u.email!r}: industry={industry!r} -> sector={resolved!r}")
                seeded += 1
                continue

            # sync_vendor_sector commits per row and swallows/rolls back its own
            # failures, so one bad row cannot abort the sweep.
            if sync_vendor_sector(db, u.id, industry):
                print(f"  seeded {u.email!r}: {industry!r} -> {resolved!r}")
                seeded += 1

        print()
        print(f"{'[DRY RUN] ' if args.dry_run else ''}Backfill summary")
        print(f"  accounts scanned:            {len(users)}")
        print(f"  already had a sector:        {skipped}")
        print(f"  seeded from User.industry:   {seeded}")
        print(f"  no industry on file:         {len(unresolvable)}")

        if unresolvable:
            print()
            print("  These cannot be backfilled — no industry to infer from. They need")
            print("  the sector prompt at their next Tender Intelligence touchpoint.")
            if args.show_unresolvable:
                for u in unresolvable:
                    print(f"    - {u.email}")
            else:
                print("  Re-run with --show-unresolvable to list them.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
