"""
GeBIZ Service
=============
Fetches open tenders from GeBIZ via RSS and persists them to the database.

Rate limiting: minimum 30-second gap between scrape calls (enforced by the
Celery beat schedule — do not call scrape_gebiz_page() outside of the task).

robots.txt compliance: we only read the public RSS feed and the publicly
accessible "Open Tenders" listing; we do not crawl deeper pages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.models_gebiz import GebizTender

logger = logging.getLogger(__name__)

GEBIZ_RSS_URL = "https://www.gebiz.gov.sg/rss/opportunities.xml"
GEBIZ_OPEN_TENDERS_URL = "https://www.gebiz.gov.sg/ptt/menu/ITTWorkspaceForPublic.xhtml"

_HEADERS = {
    "User-Agent": "BooppaBot/1.0 (+https://booppa.io)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _parse_closing_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%d %b %Y %H:%M", "%d %b %Y", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def fetch_from_rss(db: Session) -> int:
    """
    Parse the GeBIZ RSS feed and upsert tenders into the database.
    Returns the number of tenders upserted.
    """
    try:
        response = httpx.get(GEBIZ_RSS_URL, headers=_HEADERS, timeout=30, follow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:
        logger.error(f"[GeBIZ] RSS fetch failed: {exc}")
        return 0

    if feed.bozo:
        logger.warning(f"[GeBIZ] RSS feed parse warning: {feed.bozo_exception}")

    count = 0
    now = datetime.utcnow()

    for entry in feed.entries:
        tender_no = getattr(entry, "id", None) or getattr(entry, "link", None) or ""
        title = getattr(entry, "title", "").strip()
        url = getattr(entry, "link", None)
        agency = getattr(entry, "author", "") or getattr(entry, "source", {}).get("title", "")
        closing_raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
        closing_date = _parse_closing_date(closing_raw)

        if not tender_no or not title:
            continue

        raw_data = {
            "summary": getattr(entry, "summary", ""),
            "tags": [t.get("term", "") for t in getattr(entry, "tags", [])],
        }

        existing = db.query(GebizTender).filter(GebizTender.tender_no == tender_no).first()
        if existing:
            existing.title = title
            existing.agency = agency or existing.agency
            existing.closing_date = closing_date or existing.closing_date
            existing.url = url or existing.url
            existing.raw_data = raw_data
            existing.last_fetched_at = now
        else:
            db.add(GebizTender(
                tender_no=tender_no,
                title=title,
                agency=agency,
                closing_date=closing_date,
                status="Open",
                url=url,
                raw_data=raw_data,
                last_fetched_at=now,
            ))
        count += 1

    db.commit()
    logger.info(f"[GeBIZ] RSS sync upserted {count} tenders")
    return count


def scrape_gebiz_page(db: Session) -> int:
    """
    Lightweight scrape of the GeBIZ public Open Tenders listing.
    Returns the number of additional tenders upserted (not already in RSS).

    Only the public listing page is fetched — no deep crawling.
    """
    try:
        response = httpx.get(GEBIZ_OPEN_TENDERS_URL, headers=_HEADERS, timeout=30, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"[GeBIZ] Scrape HTTP error: {exc}")
        return 0

    soup = BeautifulSoup(response.text, "lxml")
    now = datetime.utcnow()
    count = 0

    # GeBIZ renders a table with class "listTable" or similar; rows contain tender info.
    rows = soup.select("table.listTable tr, table.dataTable tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        tender_no = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        agency = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        closing_raw = cells[3].get_text(strip=True) if len(cells) > 3 else None
        closing_date = _parse_closing_date(closing_raw)

        link_tag = cells[1].find("a")
        url = link_tag["href"] if link_tag and link_tag.get("href") else None
        if url and not url.startswith("http"):
            url = "https://www.gebiz.gov.sg" + url

        if not tender_no or not title:
            continue

        existing = db.query(GebizTender).filter(GebizTender.tender_no == tender_no).first()
        if existing:
            existing.last_fetched_at = now
            if closing_date:
                existing.closing_date = closing_date
        else:
            db.add(GebizTender(
                tender_no=tender_no,
                title=title,
                agency=agency,
                closing_date=closing_date,
                status="Open",
                url=url,
                last_fetched_at=now,
            ))
            count += 1

    db.commit()
    logger.info(f"[GeBIZ] Scrape upserted {count} new tenders")
    return count
