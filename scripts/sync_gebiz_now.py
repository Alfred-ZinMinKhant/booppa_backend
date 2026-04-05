#!/usr/bin/env python3
"""
sync_gebiz_now.py — Manually trigger a GeBIZ RSS sync
======================================================
Fetches live tenders from the GeBIZ RSS feed and upserts them into
the gebiz_tenders table immediately, without waiting for Celery Beat.

Usage:
    python scripts/sync_gebiz_now.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.services.gebiz_service import fetch_from_rss, scrape_gebiz_page


def main() -> None:
    db = SessionLocal()
    try:
        count = fetch_from_rss(db)
        print(f"[GeBIZ] RSS sync — {count} tender(s) upserted.")
        if count == 0:
            print("[GeBIZ] RSS returned 0 — falling back to page scrape.")
            count = scrape_gebiz_page(db)
            print(f"[GeBIZ] Scrape sync — {count} tender(s) upserted.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
