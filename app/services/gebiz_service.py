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
import re
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.models_gebiz import GebizTender

logger = logging.getLogger(__name__)

_GEBIZ_RSS_BASE = "https://www.gebiz.gov.sg/rss/{category}-CREATE_BO_FEED.xml"

# GeBIZ restructured RSS into per-category feeds in 2025. The old single URL
# (rss/opportunities.xml) is dead. Each feed covers tenders published in the
# last 2 days for that category.
_GEBIZ_RSS_CATEGORIES = [
    "Professional_Services",
    "IT_%26_Telecommunication",
    "Security_Services",
    "Maintenance_Services",
    "Environmental_Services",
    "Training_Services",
    "Medical_%26_Healthcare",
    "Marketing_%26_Advertising",
    "Research_%26_Development",
    "General_Building_%26_Minor_Construction_Works",
    "Facilities_Management",
    "Transportation",
    "Administration_%26_Training",
    "Event_Organising_Food_%26_Beverages",
    "Furniture_Office_Equipment_%26_AudioVisual",
    "Miscellaneous",
    "Works",
    "Consultancy_Services",
]

GEBIZ_OPEN_TENDERS_URL = "https://www.gebiz.gov.sg/ptt/menu/ITTWorkspaceForPublic.xhtml"

_HEADERS = {
    "User-Agent": "BooppaBot/1.0 (+https://booppa.io)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Regex patterns for parsing the description field ──
# Example description:
#   "ITQ: CDVHQ0ETQ26000008 | Published Date: 07/04/2026 | Closing Date: 16/04/2026 13:00:00 | Calling Entity: Ministry of Social and Family Development |"
_RE_TENDER_NO = re.compile(r"^(?:ITQ|ITT|RFQ|RFP|EOI)\s*:\s*(\S+)", re.IGNORECASE)
_RE_CLOSING_DATE = re.compile(r"Closing\s+Date\s*:\s*([\d/]+ [\d:]+|[\d/]+)", re.IGNORECASE)
_RE_AGENCY = re.compile(r"Calling\s+Entity\s*:\s*(.+?)(?:\s*\||\s*$)", re.IGNORECASE)


def _parse_closing_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in (
        "%d/%m/%Y %H:%M:%S",  # GeBIZ description format: 16/04/2026 13:00:00
        "%d/%m/%Y %H:%M",     # Without seconds
        "%d/%m/%Y",           # Date only
        "%d %b %Y %H:%M",    # e.g. 16 Apr 2026 13:00
        "%d %b %Y",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_description(description: str) -> dict:
    """
    Parse the GeBIZ RSS description field to extract structured data.
    Returns dict with keys: tender_no, closing_date, agency (all optional).
    """
    result = {}

    if not description:
        return result

    m = _RE_TENDER_NO.search(description)
    if m:
        result["tender_no"] = m.group(1).strip()

    m = _RE_CLOSING_DATE.search(description)
    if m:
        result["closing_date"] = _parse_closing_date(m.group(1).strip())

    m = _RE_AGENCY.search(description)
    if m:
        result["agency"] = m.group(1).strip()

    return result


def fetch_from_rss(db: Session) -> int:
    """
    Fetch all GeBIZ per-category RSS feeds and upsert tenders into the database.
    Returns the total number of tenders upserted across all categories.
    """
    count = 0
    now = datetime.now(timezone.utc)

    for category in _GEBIZ_RSS_CATEGORIES:
        feed_url = _GEBIZ_RSS_BASE.format(category=category)
        try:
            response = httpx.get(feed_url, headers=_HEADERS, timeout=15, follow_redirects=True)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            logger.warning(f"[GeBIZ] RSS fetch failed for {category}: {exc}")
            continue

        if feed.bozo and feed.bozo_exception:
            logger.debug(f"[GeBIZ] RSS parse warning for {category}: {feed.bozo_exception}")

        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            entry_url = getattr(entry, "link", None)

            # Skip placeholder "no RSS feed available" entries
            if not title or "no rss feed available" in title.lower():
                continue

            # Parse the description field to get structured data
            description = getattr(entry, "summary", "") or getattr(entry, "description", "")
            parsed = _parse_description(description)

            # tender_no: prefer parsed from description, fall back to entry id/link
            tender_no = parsed.get("tender_no") or ""
            if not tender_no:
                raw_id = getattr(entry, "id", None) or entry_url or ""
                # Try to extract code param from URL: ...?code=CDVHQ0ETQ26000008&...
                code_match = re.search(r"[?&]code=([^&]+)", raw_id)
                tender_no = code_match.group(1) if code_match else raw_id

            if not tender_no:
                continue

            # closing_date: from parsed description
            closing_date = parsed.get("closing_date")

            # agency: from parsed description, fall back to feed author
            agency = parsed.get("agency") or getattr(entry, "author", "") or ""

            raw_data = {
                "summary": description,
                "tags": [t.get("term", "") for t in getattr(entry, "tags", [])],
                "category": category.replace("_", " ").replace("%26", "&"),
            }

            existing = db.query(GebizTender).filter(GebizTender.tender_no == tender_no).first()
            if existing:
                existing.title = title
                existing.agency = agency or existing.agency
                existing.closing_date = closing_date or existing.closing_date
                existing.url = entry_url or existing.url
                existing.raw_data = raw_data
                existing.last_fetched_at = now
            else:
                db.add(GebizTender(
                    tender_no=tender_no,
                    title=title,
                    agency=agency,
                    closing_date=closing_date,
                    status="Open",
                    url=entry_url,
                    raw_data=raw_data,
                    last_fetched_at=now,
                ))
            count += 1

    db.commit()
    logger.info(f"[GeBIZ] RSS sync upserted {count} tenders across {len(_GEBIZ_RSS_CATEGORIES)} categories")
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
    now = datetime.now(timezone.utc)
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


def ensure_tenders_loaded(db: Session) -> int:
    """
    On-demand sync: if the DB has no open tenders, fetch from RSS immediately.
    Called by the API endpoint as a fallback when Celery Beat hasn't run yet.
    Returns the count of open tenders after the check.
    """
    open_count = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .count()
    )
    if open_count > 0:
        return open_count

    logger.info("[GeBIZ] No open tenders in DB — triggering on-demand RSS sync")
    try:
        rss_count = fetch_from_rss(db)
        logger.info(f"[GeBIZ] On-demand sync upserted {rss_count} tenders")
        return rss_count
    except Exception as exc:
        logger.error(f"[GeBIZ] On-demand sync failed: {exc}")
        db.rollback()
        return 0
