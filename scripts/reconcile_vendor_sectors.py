#!/usr/bin/env python3
"""
reconcile_vendor_sectors.py — collapse accumulated / contradictory sector rows
==============================================================================
`vendor_sectors` was append-only: every write path added a row and none ever
removed one, and every reader did an unordered `.first()`. A vendor whose
industry changed — or who was ever touched by an admin simulate-purchase, a
GeBIZ "Works" category, or an early test checkout — kept the old row forever,
and which one won a given report was left to Postgres.

That is how matchmove, a MAS-licensed payments company (MPI, PS20200239), was
issued a Competitor Awareness Signals report headed CONSTRUCTION. Nothing
errored: "Construction" lowercases into a valid `GebizAwardHistory.sector` key,
so the report filled with a complete and plausible Construction competitor set.

`sync_vendor_sector` (additive) is now reserved for genuinely multi-sector facts;
identity-bearing writes go through `set_vendor_sector`, which replaces. This
script cleans up what the old behaviour already left behind.

Two problems are reported:

  DUPLICATE     — more than one row for the vendor. No reader can pick between
                  them deterministically.
  CONTRADICTORY — the row(s) disagree with the account's own `User.industry`,
                  which is the identity fact we actually hold.

Only CONTRADICTORY rows with a `User.industry` to defer to can be repaired
automatically. A vendor with several rows and no industry on file has no
authority to collapse toward, and is listed rather than guessed at — picking one
would recreate the exact failure this script exists to undo.

Usage:
    python scripts/reconcile_vendor_sectors.py                 # dry run (default)
    python scripts/reconcile_vendor_sectors.py --apply         # write changes
    python scripts/reconcile_vendor_sectors.py --email a@b.co  # inspect one account

Against production, reach the VPC-only RDS instance through scripts/db_tunnel.sh
first — booppa-postgres is not publicly accessible.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User  # noqa: F401 — import before models that FK to users
from app.core.models import VendorSector
from app.services.tender_service import normalise_sector, set_vendor_sector


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--email", help="restrict to a single account")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(User)
        if args.email:
            q = q.filter(User.email == args.email)
        users = q.all()

        by_vendor: dict = {}
        for row in db.query(VendorSector).all():
            by_vendor.setdefault(row.vendor_id, []).append(row)

        clean = repaired = 0
        needs_a_human: list[tuple] = []

        for u in users:
            rows = by_vendor.get(u.id) or []
            if not rows:
                continue

            sectors = [(r.sector or "").strip() for r in rows if (r.sector or "").strip()]
            industry = normalise_sector(getattr(u, "industry", None))
            duplicate = len(sectors) > 1
            contradictory = bool(
                industry and not any(s.lower() == industry.lower() for s in sectors)
            )

            if not duplicate and not contradictory:
                clean += 1
                continue

            flags = []
            if duplicate:
                flags.append("DUPLICATE")
            if contradictory:
                flags.append("CONTRADICTORY")

            print(f"{'|'.join(flags):26} {u.email}")
            print(f"{'':26}   rows     = {sectors}")
            print(f"{'':26}   industry = {industry!r}")

            if not industry:
                # Nothing authoritative to collapse toward.
                needs_a_human.append((u, sectors))
                print(f"{'':26}   -> no industry on file; leaving alone")
                continue

            if args.apply:
                set_vendor_sector(db, u.id, industry)
                print(f"{'':26}   -> collapsed to {industry!r}")
            else:
                print(f"{'':26}   -> would collapse to {industry!r}")
            repaired += 1

        print()
        print(f"{'' if args.apply else '[DRY RUN] '}Reconciliation summary")
        print(f"  accounts scanned:          {len(users)}")
        print(f"  already consistent:        {clean}")
        print(f"  {'repaired' if args.apply else 'repairable'}:{'':17} {repaired}")
        print(f"  need a human decision:     {len(needs_a_human)}")

        if needs_a_human:
            print()
            print("  These have conflicting rows and no User.industry to defer to.")
            print("  Set the account's industry, then re-run — do not pick one by hand")
            print("  without checking what the vendor actually does.")
            for u, sectors in needs_a_human:
                print(f"    - {u.email}: {sectors}")

        if not args.apply and repaired:
            print()
            print("  Dry run — re-run with --apply to write these changes.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
