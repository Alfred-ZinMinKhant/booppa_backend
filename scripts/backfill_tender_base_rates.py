#!/usr/bin/env python
"""One-off backfill for TenderShortlist.base_rate.

Runs the same `refresh_gebiz_base_rates` logic used by the weekly Celery beat
job, synchronously, so existing shortlist rows stuck at the 0.20 default get
recalibrated to real per sector/agency GeBIZ award rates immediately instead of
waiting for the next scheduled run.

Usage:
    python scripts/backfill_tender_base_rates.py

Non-destructive: only updates base_rate where it drifts >0.005 from the
computed rate. Safe to re-run.
"""

import logging

logging.basicConfig(level=logging.INFO)


def main() -> None:
    from app.workers.tasks import refresh_gebiz_base_rates

    # `refresh_gebiz_base_rates` is a Celery task wrapping a plain function; call
    # it directly (synchronously) rather than dispatching to a worker.
    refresh_gebiz_base_rates()
    print("[backfill] base_rate refresh complete — see logs for per-row counts.")


if __name__ == "__main__":
    main()
