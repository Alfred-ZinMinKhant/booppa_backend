"""
Vendor Website & Email Scraper
==============================
Resolves vendor websites and extracts contact emails.
No external AI — uses regex + prefix heuristics.

Pipeline:
  1. Resolve website URL from domain/company name
  2. Crawl homepage, /contact, /about (respect robots.txt)
  3. Extract emails via regex, categorise by prefix
  4. Persist to MarketplaceVendor / DiscoveredVendor
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy.orm import Session

from app.core.models_v10 import MarketplaceVendor, DiscoveredVendor

logger = logging.getLogger(__name__)

USER_AGENT = "BooppaContactBot/1.0 (+https://www.booppa.io)"
REQUEST_TIMEOUT = 10.0
MAX_PAGES_PER_VENDOR = 4
SCRAPE_COOLDOWN_DAYS = 30

# Paths to crawl for contact info
CONTACT_PATHS = ["/", "/contact", "/contact-us", "/about", "/about-us"]

# Email regex — standard RFC-ish pattern
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# Junk email patterns to skip
JUNK_PATTERNS = re.compile(
    r"(example\.com|sentry\.io|wixpress|wordpress|gravatar|"
    r"schema\.org|w3\.org|googleusercontent|fbcdn|placeholder|"
    r"\.png|\.jpg|\.gif|\.svg|\.css|\.js)$",
    re.IGNORECASE,
)

# Email category by prefix
DPO_PREFIXES = {"dpo", "privacy", "dataprotection", "data.protection", "pdpa"}
GENERAL_PREFIXES = {"info", "contact", "hello", "enquiry", "enquiries", "sales", "support", "admin"}


def _categorise_email(email: str) -> str:
    """Categorise email: dpo, general, or other."""
    prefix = email.split("@")[0].lower()
    if any(prefix.startswith(p) for p in DPO_PREFIXES):
        return "dpo"
    if any(prefix.startswith(p) for p in GENERAL_PREFIXES):
        return "general"
    return "other"


def _is_junk_email(email: str) -> bool:
    """Filter out non-contact emails (image filenames, framework domains, etc.)."""
    return bool(JUNK_PATTERNS.search(email))


def _extract_emails(html: str) -> list[dict]:
    """Extract and dedupe emails from HTML, categorised by prefix."""
    raw = EMAIL_RE.findall(html)
    seen = set()
    results = []
    for email in raw:
        email_lower = email.lower()
        if email_lower in seen or _is_junk_email(email_lower):
            continue
        seen.add(email_lower)
        results.append({
            "email": email_lower,
            "category": _categorise_email(email_lower),
        })
    return results


async def _check_robots(base_url: str, path: str) -> bool:
    """Check if path is allowed by robots.txt. Returns True if allowed or robots.txt unavailable."""
    try:
        rp = RobotFileParser()
        rp.set_url(f"{base_url}/robots.txt")
        # Fetch robots.txt manually since RobotFileParser.read() is blocking
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/robots.txt", headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                return rp.can_fetch(USER_AGENT, path)
    except Exception:
        pass
    return True  # allow if robots.txt is unreachable


async def _fetch_page(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Fetch a single page, return HTML or None."""
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        if resp.status_code < 400 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text
    except Exception as e:
        logger.debug(f"[Scraper] Failed to fetch {url}: {e}")
    return None


async def resolve_website(company_name: str, domain: Optional[str] = None) -> Optional[str]:
    """
    Resolve a vendor's website URL.
    Priority: existing domain → construct from company name → None.
    """
    candidates = []

    if domain:
        cleaned = domain.strip().lower()
        if not cleaned.startswith("http"):
            candidates.append(f"https://{cleaned}")
            candidates.append(f"http://{cleaned}")
        else:
            candidates.append(cleaned)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for url in candidates:
            try:
                resp = await client.head(url, headers={"User-Agent": USER_AGENT})
                if resp.status_code < 400:
                    return str(resp.url)
            except Exception:
                continue

    return None


async def scrape_vendor_contacts(website_url: str) -> dict:
    """
    Crawl a vendor website and extract contact information.

    Returns:
        {
            "emails": [{"email": "...", "category": "general|dpo|other"}, ...],
            "primary_email": "info@...",
            "dpo_email": "dpo@..." or None,
            "pages_crawled": 3,
            "crawled_at": "2026-04-20T...",
        }
    """
    parsed = urlparse(website_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    all_emails: list[dict] = []
    pages_crawled = 0

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2),
    ) as client:
        for path in CONTACT_PATHS:
            if pages_crawled >= MAX_PAGES_PER_VENDOR:
                break

            full_url = urljoin(base_url, path)

            # Respect robots.txt
            if not await _check_robots(base_url, path):
                logger.debug(f"[Scraper] Blocked by robots.txt: {full_url}")
                continue

            html = await _fetch_page(client, full_url)
            if html:
                pages_crawled += 1
                all_emails.extend(_extract_emails(html))

            # Rate limit: 1 req/sec per domain
            await asyncio.sleep(1.0)

    # Dedupe across pages
    seen = set()
    deduped = []
    for entry in all_emails:
        if entry["email"] not in seen:
            seen.add(entry["email"])
            deduped.append(entry)

    # Pick primary email: prefer general, then dpo, then first other
    primary = None
    dpo = None
    for e in deduped:
        if e["category"] == "general" and not primary:
            primary = e["email"]
        if e["category"] == "dpo" and not dpo:
            dpo = e["email"]

    if not primary and deduped:
        primary = deduped[0]["email"]

    return {
        "emails": deduped,
        "primary_email": primary,
        "dpo_email": dpo,
        "pages_crawled": pages_crawled,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def scrape_and_update_vendor(db: Session, vendor_id: str, model: str = "marketplace") -> dict:
    """
    Synchronous wrapper: resolve website, scrape contacts, update DB.
    Called from Celery task.

    Args:
        db: SQLAlchemy session
        vendor_id: UUID string
        model: "marketplace" or "discovered"

    Returns:
        {"status": "ok"|"skipped"|"no_website"|"error", ...}
    """
    Model = MarketplaceVendor if model == "marketplace" else DiscoveredVendor
    vendor = db.query(Model).filter(Model.id == vendor_id).first()
    if not vendor:
        return {"status": "error", "reason": "vendor_not_found"}

    # Check cooldown
    if vendor.last_scraped_at:
        from datetime import timedelta
        if (datetime.now(timezone.utc) - vendor.last_scraped_at).days < SCRAPE_COOLDOWN_DAYS:
            return {"status": "skipped", "reason": "recently_scraped"}

    # Resolve website if missing
    website = getattr(vendor, "website", None) or None
    domain = getattr(vendor, "domain", None) or None

    if not website and not domain:
        return {"status": "no_website", "reason": "no_website_or_domain"}

    async def _run():
        resolved = website
        if not resolved:
            resolved = await resolve_website(vendor.company_name, domain)
        if not resolved:
            return {"status": "no_website", "reason": "could_not_resolve"}

        # Update website field if it was empty
        if not website and resolved:
            vendor.website = resolved

        result = await scrape_vendor_contacts(resolved)
        return result

    try:
        result = asyncio.run(_run())
    except Exception as e:
        logger.error(f"[Scraper] Failed for vendor {vendor_id}: {e}")
        return {"status": "error", "reason": str(e)[:200]}

    if result.get("status") in ("no_website",):
        return result

    # Persist results
    vendor.contact_email = result.get("primary_email")
    vendor.scraped_data = {
        "emails": result.get("emails", []),
        "dpo_email": result.get("dpo_email"),
        "pages_crawled": result.get("pages_crawled", 0),
    }
    vendor.last_scraped_at = datetime.now(timezone.utc)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[Scraper] DB commit failed for {vendor_id}: {e}")
        return {"status": "error", "reason": f"db_error: {e}"}

    return {
        "status": "ok",
        "vendor_id": vendor_id,
        "contact_email": result.get("primary_email"),
        "email_count": len(result.get("emails", [])),
        "pages_crawled": result.get("pages_crawled", 0),
    }
