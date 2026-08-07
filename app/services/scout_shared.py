"""
scout_shared.py
Booppa Smart Care LLC — SCOUT Agents, shared utilities

Fixes four concrete bugs found in the original three scripts during
review, all corrected here so every pipeline inherits the fix instead of
patching each script separately:

1. WRONG ACRA DATASET ID. scout_v2_fixed.py hardcoded
   ACRA_RESOURCE_ID = "5ab68aac-91f6-4f39-9b21-698610bdf3f8" — not the real
   dataset. Fixed: reuse settings.ACRA_DATASET_ID
   ("d_3f960c10fed6145404ca7b821f263b87"), the exact same constant
   acra_service.py and evidence_enricher.py already use — one source of
   truth, not a second hardcoded copy that can drift out of sync again.

2. FABRICATED SSL GRADE. The original _check_ssl() always returned grade
   "B" for any HTTPS site, regardless of actual configuration — a
   constant dressed up as an assessment. Fixed: report only what's
   genuinely checkable for free from a normal TLS handshake (protocol
   version, days until certificate expiry) — no invented letter grade.

3. verify=False ON EVERY REQUEST. Disabled TLS certificate validation
   globally, which is inconsistent with trying to assess TLS quality and
   is bad practice on principle. Fixed: verify=True by default; a fetch
   that fails due to a certificate problem is itself a real, reportable
   PDPA §24 finding ("certificate invalid or expired"), not something to
   paper over by disabling the check.

4. NO EXPONENTIAL BACKOFF. The original scripts used a flat
   time.sleep(REQUEST_DELAY) and gave up after one failure per page.
   acra_service.py's real fetcher already has the correct pattern (retry
   on 429/5xx with exponential backoff honouring Retry-After) — reused
   here instead of reinventing a weaker version.

COST DISCIPLINE (explicit, since it's a hard requirement): every function
in this file calls only free, already-used data sources (data.gov.sg) or
does a plain HTTP GET against a vendor's own public website. No paid
search API, no paid SSL-grading service (SSL Labs' API is free but rate
limited to the point of being impractical at this volume — not used).
Nothing here adds a new paid dependency to the stack.
"""

import re
import ssl as ssl_module
import socket
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from typing import Optional

import httpx

from app.core.config import settings

DATA_GOV_BASE = "https://data.gov.sg/api/action/datastore_search"
PAGE_SIZE = 100
MAX_PAGE_RETRIES = 4
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "BooppaBot/1.0 (+https://www.booppa.io)"

# FIX #1 — reuse the real dataset ID, not a second hardcoded guess.
ACRA_DATASET_ID = settings.ACRA_DATASET_ID
GEBIZ_DATASET_ID = "d_acde1106003906a75c3fa052592f2fcb"  # confirmed correct in the original scripts, unchanged


async def fetch_datastore_page(client: httpx.AsyncClient, dataset_id: str,
                                 offset: int, limit: int = PAGE_SIZE,
                                 filters: Optional[dict] = None,
                                 q: Optional[str] = None) -> dict:
    """
    FIX #4 — same paginated-fetch-with-backoff pattern already proven in
    acra_service.py, reused rather than reimplemented. Async (not sync
    httpx.Client like the originals) so this can run inside the async
    Celery task context the rest of this backend already uses for
    external calls.
    """
    import json as _json

    params = {"resource_id": dataset_id, "limit": limit, "offset": offset}
    if filters:
        params["filters"] = _json.dumps(filters)
    if q:
        params["q"] = q

    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        try:
            r = await client.get(DATA_GOV_BASE, params=params, timeout=30,
                                  headers={"User-Agent": USER_AGENT})
            if r.status_code in RETRYABLE_STATUS:
                retry_after = float(r.headers.get("Retry-After", 2 ** attempt))
                if attempt == MAX_PAGE_RETRIES:
                    return {"result": {"records": [], "total": 0}}
                await asyncio.sleep(retry_after)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            if attempt == MAX_PAGE_RETRIES:
                return {"result": {"records": [], "total": 0}}
            await asyncio.sleep(2 ** attempt)

    return {"result": {"records": [], "total": 0}}


async def fetch_all_datastore_records(dataset_id: str, filters: Optional[dict] = None,
                                        q: Optional[str] = None,
                                        max_records: Optional[int] = None) -> list[dict]:
    """Full pagination sweep — same shape as refresh_gebiz_base_rates' while-True loop."""
    records: list[dict] = []
    offset = 0

    async with httpx.AsyncClient() as client:
        while True:
            data = await fetch_datastore_page(client, dataset_id, offset, filters=filters, q=q)
            page = data.get("result", {}).get("records", [])
            if not page:
                break
            records.extend(page)
            offset += PAGE_SIZE
            total = data.get("result", {}).get("total", 0)
            if offset >= total or (max_records and len(records) >= max_records):
                break
            await asyncio.sleep(0.6)  # same polite inter-page delay as acra_service.py

    return records[:max_records] if max_records else records


def normalise_company_name(name: str) -> str:
    """Same normalisation logic as the original scripts — this part was fine, kept as-is."""
    if not name:
        return ""
    n = name.upper().strip()
    n = re.sub(r'\bPTE\.?\s*LTD\.?\b', 'PTE LTD', n)
    n = re.sub(r'\bPRIVATE\s+LIMITED\b', 'PTE LTD', n)
    n = re.sub(r'\bSDN\.?\s*BHD\.?\b', 'SDN BHD', n)
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def prospect_natural_key(uen: str, clean_name: str) -> str:
    """
    The key ScoutProspect UPSERTs on. UEN when known (stable, unique,
    correct choice) — falls back to normalised name only when no UEN was
    resolved (weaker, but the original scripts already produced
    prospects with an empty UEN in that case, so a fallback is needed
    rather than silently dropping them).
    """
    return uen.strip().upper() if uen and uen.strip() else f"NAME::{normalise_company_name(clean_name)}"


async def check_website_candidate(client: httpx.AsyncClient, url: str,
                                   expected_name_tokens: list[str]) -> Optional[str]:
    """
    FIX #3 (verify=True, default) + a stricter match than the originals.

    The original heuristic accepted ANY 200 response over 1000/800 chars
    as a match — a parked domain, an unrelated company with a similar
    name, or a generic hosting placeholder page could all pass that bar.
    This requires at least one real name token to actually appear in the
    page text, always — not "even if not found, a 200 is good enough"
    like the original. Stricter means fewer websites get matched overall
    (a real trade-off, not a free improvement) — but a wrong match
    propagates into a wrong PDPA scan and a wrong outreach email, which
    is a worse outcome than matching fewer vendors correctly.
    """
    try:
        r = await client.get(url, timeout=8, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return None
        text_lower = r.text.lower()
        matches = sum(1 for tok in expected_name_tokens if len(tok) > 3 and tok in text_lower)
        if matches == 0:
            return None
        return str(r.url)
    except httpx.HTTPError:
        return None


async def find_website_heuristic(company_name: str, extra_strip_words: tuple[str, ...] = (),
                                   tld_patterns: tuple[str, ...] = (".com.sg", ".sg", ".com")) -> str:
    """
    Same domain-guessing strategy as the originals (no paid search API —
    cost discipline requirement), but now verify=True and the stricter
    match above. Async, single shared client per call for connection reuse.
    """
    strip_words = ("pte", "ltd", "llp", "inc", "corp", "sdn", "bhd") + extra_strip_words
    pattern = r'\b(' + '|'.join(strip_words) + r')\b'
    domain_base = re.sub(pattern, '', company_name.lower(), flags=re.IGNORECASE)
    domain_base = re.sub(r'[^\w]', '', domain_base).strip()[:30]
    if not domain_base:
        return ""

    name_tokens = [t for t in re.split(r'\W+', company_name.lower()) if t]

    candidates = [f"https://www.{domain_base}{tld}" for tld in tld_patterns]
    candidates += [f"https://{domain_base}{tld}" for tld in tld_patterns]

    async with httpx.AsyncClient(verify=True) as client:  # FIX #3
        for url in candidates:
            match = await check_website_candidate(client, url, name_tokens)
            if match:
                return match
    return ""


def check_tls_honestly(url: str, timeout: float = 6.0) -> dict:
    """
    FIX #2 — replaces the fabricated always-"B" grade. Reports only what
    a plain TLS handshake genuinely reveals, for free, with no paid
    SSL-grading service:
      - https_used: whether the URL scheme is https
      - handshake_ok: whether a real TLS handshake actually succeeded
        (catches expired/invalid/self-signed certs — the original never
        checked this at all, since verify=False made every handshake
        "succeed")
      - days_until_expiry: certificate expiry, a genuine, checkable fact
      - tls_version: the negotiated protocol version (e.g. "TLSv1.3")

    No letter grade is returned. If Booppa's product/marketing needs a
    single simplified label later, derive it explicitly from these real
    facts (e.g. "days_until_expiry < 14" is a legitimate finding) rather
    than reintroducing a constant.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {"https_used": False, "handshake_ok": False,
                "days_until_expiry": None, "tls_version": None}

    host = parsed.hostname
    port = parsed.port or 443
    ctx = ssl_module.create_default_context()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                tls_version = ssock.version()
                not_after = cert.get("notAfter")
                days_left = None
                if not_after:
                    expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                    days_left = (expiry.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
                return {
                    "https_used": True, "handshake_ok": True,
                    "days_until_expiry": days_left, "tls_version": tls_version,
                }
    except (ssl_module.SSLError, socket.error, socket.timeout, ConnectionRefusedError):
        # A failed handshake IS the finding — a real, expired, or
        # misconfigured certificate is a genuine PDPA §24 gap, not
        # something to suppress by disabling verification.
        return {"https_used": True, "handshake_ok": False,
                "days_until_expiry": None, "tls_version": None}


def extract_contact_email(html: str, company_domain: str) -> Optional[str]:
    """
    GAP THIS CLOSES: none of the original three scripts ever populated a
    real destination email address — every outreach template used
    "Dear [Name]," as a manual-fill placeholder. Without a real address,
    an automated send task has nothing to send to, and every approved
    prospect would sit forever without ever actually being contacted —
    silently defeating the whole point of automating this. This is a
    free, best-effort extraction from HTML already fetched during the
    PDPA/AML scan step (no new request, no new cost), not a separate
    paid enrichment lookup.

    Strategy, in order of preference:
      1. A mailto: link containing the company's own domain (most
         reliable — it's an address the company itself published)
      2. A generic prefix (info@ / enquiry@ / contact@ / sales@) at the
         company's own domain, if literally present as text on the page
      3. None — the send task correctly leaves the prospect APPROVED but
         unsent, surfaced in the weekly digest for a human to add a real
         contact rather than guessing one that doesn't exist.
    """
    mailto_matches = re.findall(r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', html)
    domain_root = company_domain.replace("www.", "")

    for addr in mailto_matches:
        if domain_root and domain_root in addr.lower():
            return addr

    if mailto_matches:
        return mailto_matches[0]  # any mailto found, even off-domain (e.g. a shared group inbox) beats nothing

    for prefix in ("info", "enquiry", "enquiries", "contact", "sales", "hello"):
        guess = f"{prefix}@{domain_root}"
        if guess.lower() in html.lower():
            return guess

    return None


def redis_pipeline_lock(redis_client, lock_key: str, ttl_seconds: int) -> bool:
    """
    Same SETNX auto-expiring lock already used by sync_gebiz_tenders() —
    prevents overlapping runs of the same pipeline if a beat tick queues
    a second run before the first finishes. Reused verbatim, not
    reinvented.
    """
    try:
        return bool(redis_client.set(lock_key, "1", nx=True, ex=ttl_seconds))
    except Exception:
        return True  # best-effort — same fallback behaviour as the existing pattern
