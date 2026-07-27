#!/usr/bin/env python3
"""Audit and reset the cached ACRA legal identity (User.legal_name / User.uen)
on accounts whose cache got contaminated by the pre-fix, account-scoped resolver.

Background: evidence_enricher.resolve_legal_name() used to persist a resolved
legal name/UEN onto the User row and short-circuit forever after, and
_fulfill_vendor_proof() wrote back directly with no trust check at all. A reused
QA/test account could therefore carry a stale identity (e.g. "SPQR Communications",
UEN 21374700J) that then leaked into reports about entirely different vendors —
and, via the Vendor Proof write-back, got re-confirmed on every run.

The code fix routes every write through evidence_enricher.record_verified_identity(),
which records provenance in `legal_name_hint` / `website_hint`. Rows written BEFORE
that fix have a cached identity with NULL provenance — that is the signature this
script's --audit mode looks for. Clearing them lets the next resolution re-derive
the identity honestly.

Note the resolver already treats NULL provenance as untrusted, so an unaudited row
is not actively dangerous — but it also can't be told apart from a correctly-cleared
one, which is why clearing is still worth doing on known-bad accounts.

Usage:
    python scripts/reset_cached_legal_identity.py --audit
    python scripts/reset_cached_legal_identity.py --audit --reports
    python scripts/reset_cached_legal_identity.py --email a@b.com [--email c@d.com]
    python scripts/reset_cached_legal_identity.py --contains "SPQR"      # by cached value
    python scripts/reset_cached_legal_identity.py --contains "SPQR" --dry-run
"""
import argparse
import sys

from app.core.db import SessionLocal
from app.core.models import Report, User


def _audit(db, include_reports: bool) -> None:
    """List accounts holding a cached identity with no provenance (pre-fix rows)."""
    rows = (
        db.query(User)
        .filter(
            (User.legal_name.isnot(None)) | (User.uen.isnot(None)),
            User.legal_name_hint.is_(None),
        )
        .order_by(User.email)
        .all()
    )
    if not rows:
        print("No accounts with an unprovenanced cached identity. Clean.")
    else:
        print(
            f"{len(rows)} account(s) hold a cached identity with NULL legal_name_hint "
            f"(pre-fix provenance — untrusted by the resolver, and a candidate to clear):\n"
        )
        for u in rows:
            print(
                f"  {u.email}\n"
                f"      company    = {u.company!r}\n"
                f"      legal_name = {u.legal_name!r}\n"
                f"      uen        = {u.uen!r}\n"
                f"      website    = {u.website!r}"
            )

    if not include_reports:
        print(
            "\nRe-run with --reports to also list already-issued Vendor Proof reports "
            "whose ACRA block may name the wrong entity."
        )
        return

    # Already-issued certificates freeze their ACRA block into Report.assessment_data,
    # which is ALSO what the public /verify/{audit_hash} page serves. Clearing the User
    # row does not touch these — they need a separate decision (re-fulfil or void).
    #
    # `assessment_data` is a `json` column: cast(...) never matches, use .as_string().
    issued = (
        db.query(Report)
        .filter(
            Report.framework == "vendor_proof",
            Report.assessment_data["acra_verified"].as_string() == "true",
        )
        .order_by(Report.created_at.desc())
        .all()
    )
    print(
        f"\n{len(issued)} issued vendor_proof report(s) carry an ACRA match. Each is "
        f"served publicly at /verify/{{audit_hash}} — verify the UEN belongs to the "
        f"company named, and decide per report whether to re-fulfil or void:\n"
    )
    for r in issued:
        ad = r.assessment_data or {}
        print(
            f"  report_id={r.id} created={r.created_at} company={r.company_name!r} "
            f"uen={ad.get('uen') or ad.get('acra_uen')!r} audit_hash={r.audit_hash}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", action="append", default=[], help="Account email (repeatable)")
    ap.add_argument(
        "--contains",
        help="Match any account whose cached legal_name OR uen contains this substring (case-insensitive)",
    )
    ap.add_argument(
        "--audit",
        action="store_true",
        help="List accounts with an unprovenanced cached identity; write nothing",
    )
    ap.add_argument(
        "--reports",
        action="store_true",
        help="With --audit, also list already-issued vendor_proof reports carrying an ACRA match",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    args = ap.parse_args()

    if not args.audit and not args.email and not args.contains:
        ap.error("provide --audit, or at least one --email / --contains")

    db = SessionLocal()
    try:
        if args.audit:
            _audit(db, include_reports=args.reports)
            if not args.email and not args.contains:
                return 0

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
                f"{u.email}: legal_name={u.legal_name!r} uen={u.uen!r} "
                f"legal_name_hint={u.legal_name_hint!r} website_hint={u.website_hint!r}"
            )
            if not args.dry_run:
                # Clear the provenance columns too. Leaving legal_name_hint set while
                # nulling legal_name produces a row that looks like it has trustworthy
                # provenance for a value that no longer exists.
                from app.services.evidence_enricher import clear_cached_identity

                clear_cached_identity(u, db, commit=False)

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
