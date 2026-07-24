#!/usr/bin/env python3
"""Reset the cached ACRA legal identity (User.legal_name / User.uen) on accounts
whose cache got contaminated by the pre-fix, account-scoped resolver.

Background: evidence_enricher.resolve_legal_name() used to persist a resolved
legal name/UEN onto the User row and short-circuit forever after. A reused
QA/test account could therefore carry a stale identity (e.g. "SPQR Communications")
that then leaked into reports about entirely different vendors. The code fix makes
report deliverables report-scoped, but existing rows still hold the stale cache.
Nulling legal_name/uen lets the next genuine onboarding resolution re-derive them.

Usage:
    python scripts/reset_cached_legal_identity.py --email a@b.com [--email c@d.com]
    python scripts/reset_cached_legal_identity.py --contains "SPQR"      # by cached value
    python scripts/reset_cached_legal_identity.py --contains "SPQR" --dry-run
"""
import argparse
import sys

from app.core.db import SessionLocal
from app.core.models import User


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", action="append", default=[], help="Account email (repeatable)")
    ap.add_argument(
        "--contains",
        help="Match any account whose cached legal_name OR uen contains this substring (case-insensitive)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    args = ap.parse_args()

    if not args.email and not args.contains:
        ap.error("provide at least one --email or --contains")

    db = SessionLocal()
    try:
        q = db.query(User)
        users = []
        if args.email:
            emails = [e.strip().lower() for e in args.email]
            users.extend(q.filter(User.email.in_(emails)).all())
        if args.contains:
            needle = f"%{args.contains}%"
            users.extend(
                q.filter(
                    (User.legal_name.ilike(needle)) | (User.uen.ilike(needle))
                ).all()
            )

        # Dedupe by id, keep only rows that actually have something cached.
        seen = set()
        targets = []
        for u in users:
            if u.id in seen:
                continue
            seen.add(u.id)
            if getattr(u, "legal_name", None) or getattr(u, "uen", None):
                targets.append(u)

        if not targets:
            print("No matching accounts with a cached legal_name/uen.")
            return 0

        for u in targets:
            print(
                f"{'[dry-run] would clear' if args.dry_run else 'clearing'} "
                f"{u.email}: legal_name={u.legal_name!r} uen={u.uen!r}"
            )
            if not args.dry_run:
                u.legal_name = None
                u.uen = None

        if not args.dry_run:
            db.commit()
            print(f"Cleared cached identity on {len(targets)} account(s).")
        else:
            print(f"[dry-run] {len(targets)} account(s) would be cleared.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
