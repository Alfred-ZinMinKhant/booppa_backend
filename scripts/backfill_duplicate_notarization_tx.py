"""
One-shot: backfill tx_hash on duplicate-upload notarization Reports.

When the same SHA-256 is uploaded multiple times, the smart contract returns
"already anchored" and BlockchainService.anchor_evidence returns None — so
those Report rows have tx_hash=None even though the hash is on-chain. This
script copies the original tx_hash onto the orphans so the bundle status
endpoint counts them as anchored and the cover sheet readiness gate passes.

Usage:
    python -m scripts.backfill_duplicate_notarization_tx           # apply
    python -m scripts.backfill_duplicate_notarization_tx --dry-run # preview
"""
import sys

from app.core.db import SessionLocal
from app.core.models import Report


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    db = SessionLocal()
    try:
        orphans = (
            db.query(Report)
            .filter(
                Report.framework == "compliance_notarization",
                Report.tx_hash.is_(None),
                Report.audit_hash.isnot(None),
            )
            .all()
        )
        print(f"Found {len(orphans)} orphan notarization reports without tx_hash")

        fixed = 0
        unmatched = 0
        for r in orphans:
            prior = (
                db.query(Report)
                .filter(
                    Report.audit_hash == r.audit_hash,
                    Report.id != r.id,
                    Report.tx_hash.isnot(None),
                    Report.tx_hash != "already_anchored",
                )
                .order_by(Report.created_at.asc())
                .first()
            )
            if prior and prior.tx_hash:
                print(f"  {r.id} ← inherit tx={prior.tx_hash[:18]}… from {prior.id}")
                if not dry_run:
                    r.tx_hash = prior.tx_hash
                fixed += 1
            else:
                unmatched += 1

        if dry_run:
            print(f"\nDRY RUN — would backfill {fixed} reports ({unmatched} have no prior anchor)")
        else:
            db.commit()
            print(f"\nBackfilled {fixed} reports ({unmatched} skipped — no prior anchor found)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
