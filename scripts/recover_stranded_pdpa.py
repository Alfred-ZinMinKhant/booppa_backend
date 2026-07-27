#!/usr/bin/env python3
"""
recover_stranded_pdpa.py — Re-fulfil paid PDPA Quick Scans that never delivered
==============================================================================
A paid `pdpa_quick_scan` is "stranded" when the buyer's payment was confirmed
but no PDPA CertificateLog was ever written — i.e. no PDF, no email, no
compliance-score bump. The known cause was a completeness gate in
`_fulfill_pdpa` that read a score key the scan never wrote; this script
recovers reports left behind by that bug (and any future equivalent).

Dry-run by default: it prints what it would do and queues nothing, so no
customer email is sent until you have reviewed the list.

Usage:
    python scripts/recover_stranded_pdpa.py                     # report only
    python scripts/recover_stranded_pdpa.py --report-id <uuid>  # one report
    python scripts/recover_stranded_pdpa.py --apply             # queue fulfilment
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import CertificateLog, Report
from app.services.pdpa_findings import resolve_pdpa_risk_score


def _is_paid(assessment) -> bool:
    ad = assessment if isinstance(assessment, dict) else {}
    return bool(ad.get("payment_confirmed"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually queue fulfill_pdpa_task (default: report only)",
    )
    parser.add_argument(
        "--report-id",
        help="recover a single report id instead of scanning for all stranded ones",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Report).filter(Report.framework == "pdpa_quick_scan")
        if args.report_id:
            q = q.filter(Report.id == args.report_id)
        reports = q.order_by(Report.created_at.desc()).all()

        certified = {
            str(r[0])
            for r in db.query(CertificateLog.report_id)
            .filter(CertificateLog.certificate_type == "PDPA")
            .all()
        }

        stranded = []
        for report in reports:
            ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
            if not args.report_id and not _is_paid(ad):
                continue
            if str(report.id) in certified:
                continue
            stranded.append((report, ad))

        if not stranded:
            print("No stranded paid PDPA reports found.")
            return

        print(f"Found {len(stranded)} stranded paid PDPA report(s):\n")
        queued = 0
        for report, ad in stranded:
            email = ad.get("contact_email") or ad.get("customer_email") or "(no email)"
            risk = resolve_pdpa_risk_score(ad)
            fulfillable = risk is not None
            print(
                f"  {report.id}  status={report.status:<18} risk={risk}  "
                f"session={ad.get('stripe_session_id') or '(missing)'}  {email}"
            )
            if not fulfillable:
                print(
                    f"      SKIP — no score resolvable; the scan never completed. "
                    f"Re-run process_report_task for this report first."
                )
                continue
            if not args.apply:
                print("      would queue fulfill_pdpa_task")
                continue
            from app.workers.tasks import fulfill_pdpa_task

            fulfill_pdpa_task.delay(str(report.id), ad.get("contact_email") or ad.get("customer_email"))
            queued += 1
            print("      queued fulfill_pdpa_task")

        if args.apply:
            print(f"\nQueued {queued} fulfilment task(s).")
        else:
            print("\nDry run — nothing queued. Re-run with --apply to fulfil.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
