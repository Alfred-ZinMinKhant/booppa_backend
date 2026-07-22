#!/usr/bin/env python3
"""
scripts/backfill_legal_names.py — one-off backfill for User.legal_name
========================================================================
Resolves an ACRA-registered legal name for every user who has a `company`
or `uen` on file but no `legal_name` yet, via `evidence_enricher.resolve_legal_name`
(the same resolver now used at signup/checkout time). Leaves `company` untouched
— `legal_name` is additive, not a replacement.

Rate-limited against data.gov.sg: `fetch_acra_status` already caches results for
24h, so a re-run only pays for genuinely new lookups.

Usage:
    python scripts/backfill_legal_names.py --dry-run   # count only, no writes
    python scripts/backfill_legal_names.py              # resolve + persist
    python scripts/backfill_legal_names.py --limit 50   # cap this run
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User


async def _run(dry_run: bool, limit: int | None, delay: float) -> None:
    db = SessionLocal()
    try:
        query = db.query(User).filter(
            User.legal_name.is_(None),
            (User.company.isnot(None)) | (User.uen.isnot(None)),
        )
        if limit:
            query = query.limit(limit)
        users = query.all()

        print(f"Found {len(users)} user(s) with no legal_name to backfill.")
        if dry_run:
            for u in users:
                print(f"  [dry-run] {u.email}  company={u.company!r}  uen={u.uen!r}")
            return

        from app.services.evidence_enricher import resolve_legal_name

        resolved, unresolved = 0, 0
        for u in users:
            try:
                name = await resolve_legal_name(u, db, company_hint=u.company, uen=u.uen)
                if name and name != "Your Organisation" and name != u.company:
                    resolved += 1
                    print(f"  resolved: {u.email} -> {name}")
                else:
                    unresolved += 1
                    print(f"  no match: {u.email} (company={u.company!r})")
            except Exception as exc:
                unresolved += 1
                print(f"  error:    {u.email} — {exc}", file=sys.stderr)
            if delay:
                time.sleep(delay)

        print(f"\nDone. resolved={resolved} unresolved={unresolved} total={len(users)}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="count only, no writes/lookups")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of users processed")
    parser.add_argument("--delay", type=float, default=0.2, help="seconds to sleep between live lookups")
    args = parser.parse_args()
    asyncio.run(_run(args.dry_run, args.limit, args.delay))


if __name__ == "__main__":
    main()
