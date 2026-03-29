"""
Evidence Enricher
=================
Fetches real external data to ground RFP certificate answers in verifiable facts.

Sources:
  - ACRA live lookup      — data.gov.sg API by UEN (company status, entity type)
  - PDPC enforcement      — pdpc.gov.sg enforcement decisions list (breach history)
  - SSL Labs              — ssllabs.com API grade for vendor domain (free)
  - VirusTotal            — domain reputation / malware flags (free public API)

All results are cached in Redis (TTLs vary by source freshness requirements).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_get(key: str) -> Optional[Dict]:
    try:
        from app.core.cache import cache as c
        return c.get(c.cache_key(key))
    except Exception:
        return None


def _cache_set(key: str, value: Dict, ttl: int) -> None:
    try:
        from app.core.cache import cache as c
        c.set(c.cache_key(key), value, ttl=ttl)
    except Exception:
        pass


def _domain(vendor_url: str) -> str:
    try:
        return urlparse(vendor_url).netloc.lower().lstrip("www.")
    except Exception:
        return vendor_url


# ── 1. ACRA live lookup ────────────────────────────────────────────────────────

ACRA_DATASET_IDS = [
    "d_82ce0e3a0ce059e0a7b36c43e4cd5c96",
    "5ab68aac-91f6-4f39-9b21-698610bdf3f7",
]

async def fetch_acra_status(uen: str) -> Dict[str, Any]:
    """
    Query data.gov.sg ACRA dataset for live entity status.
    Returns: {found, live, entity_type, registered_name, registration_date, warning}
    Cached 24 h.
    """
    if not uen:
        return {"found": False}

    cache_key = f"acra_live:{uen.upper()}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    result: Dict[str, Any] = {"found": False}
    headers = {"User-Agent": "BooppaBot/1.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        for dataset_id in ACRA_DATASET_IDS:
            try:
                resp = await client.get(
                    "https://data.gov.sg/api/action/datastore_search",
                    params={"resource_id": dataset_id, "filters": f'{{"uen":"{uen.upper()}"}}', "limit": 1},
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                records = data.get("result", {}).get("records", [])
                if not records:
                    continue
                rec = records[0]
                # Normalise field names (dataset schema varies slightly)
                entity_status = (
                    rec.get("entity_status_description")
                    or rec.get("uen_status_description")
                    or rec.get("status", "")
                ).upper()
                live = "LIVE" in entity_status or entity_status == "REGISTERED"
                result = {
                    "found": True,
                    "live": live,
                    "entity_status": entity_status,
                    "entity_type": rec.get("entity_type_description") or rec.get("entity_type", ""),
                    "registered_name": rec.get("entity_name") or rec.get("company_name", ""),
                    "registration_date": rec.get("uen_issue_date") or rec.get("incorporation_date", ""),
                    "warning": None if live else f"ACRA status: {entity_status} — company may not be active",
                }
                break
            except Exception as e:
                logger.warning(f"ACRA dataset {dataset_id} query failed: {e}")

    if not result["found"]:
        result["warning"] = f"UEN {uen} not found in ACRA dataset — data may be stale or UEN incorrect"

    _cache_set(cache_key, result, ttl=86400)  # 24 h
    return result


# ── 2. PDPC enforcement check ─────────────────────────────────────────────────

PDPC_ENFORCEMENT_URL = "https://www.pdpc.gov.sg/all-enforcement-decisions"

async def fetch_pdpc_enforcement(company_name: str, uen: Optional[str] = None) -> Dict[str, Any]:
    """
    Scrape PDPC enforcement decisions list and check if vendor appears.
    Returns: {checked, found, cases: [{title, date, url}], warning}
    Cached 6 h (page doesn't change often but we want same-day freshness).
    """
    cache_key = "pdpc_enforcement_list"
    page_cache = _cache_get(cache_key)

    if not page_cache:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    PDPC_ENFORCEMENT_URL,
                    headers={"User-Agent": "BooppaBot/1.0"},
                    follow_redirects=True,
                )
            if resp.status_code == 200:
                page_cache = {"html": resp.text}
                _cache_set(cache_key, page_cache, ttl=21600)  # 6 h
        except Exception as e:
            logger.warning(f"PDPC enforcement page fetch failed: {e}")
            return {"checked": False, "found": False, "cases": []}

    if not page_cache:
        return {"checked": False, "found": False, "cases": []}

    html = page_cache.get("html", "")
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        links = soup.find_all("a", href=True)
    except ImportError:
        import re
        links = []
        # Minimal fallback: just check if company name appears in raw HTML
        name_lower = company_name.lower()
        if name_lower in html.lower():
            return {
                "checked": True,
                "found": True,
                "cases": [{"title": f"Reference to {company_name} found on PDPC enforcement page", "date": "", "url": PDPC_ENFORCEMENT_URL}],
                "warning": f"Possible PDPC enforcement action found for {company_name}. Manual review recommended.",
            }
        return {"checked": True, "found": False, "cases": []}

    name_lower = company_name.lower().strip()
    uen_upper = uen.upper() if uen else None
    found_cases = []

    for link in links:
        text = link.get_text(strip=True)
        href = link.get("href", "")
        text_lower = text.lower()
        if name_lower in text_lower or (uen_upper and uen_upper in text.upper()):
            url = href if href.startswith("http") else f"https://www.pdpc.gov.sg{href}"
            found_cases.append({"title": text, "date": "", "url": url})

    result = {
        "checked": True,
        "found": bool(found_cases),
        "cases": found_cases[:5],
        "warning": (
            f"PDPC enforcement action found for {company_name}. This should be disclosed in RFP submissions."
            if found_cases else None
        ),
    }
    return result


# ── 3. SSL Labs grade ─────────────────────────────────────────────────────────

async def fetch_ssl_grade(vendor_url: str) -> Dict[str, Any]:
    """
    Fetch SSL Labs grade for vendor domain. Uses cached results only (fromCache=on)
    so it returns immediately. Returns: {checked, grade, tls_version, warning}
    Cached 12 h.
    """
    domain = _domain(vendor_url)
    if not domain:
        return {"checked": False}

    cache_key = f"ssl_grade:{domain}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    result: Dict[str, Any] = {"checked": False, "grade": None}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.ssllabs.com/api/v3/analyze",
                params={"host": domain, "fromCache": "on", "all": "done"},
                headers={"User-Agent": "BooppaBot/1.0"},
            )
        if resp.status_code == 200:
            data = resp.json()
            endpoints = data.get("endpoints", [])
            if endpoints:
                grade = endpoints[0].get("grade", "")
                protocols = []
                for ep in endpoints:
                    for detail in (ep.get("details") or {}).get("protocols", []):
                        if detail.get("name") == "TLS":
                            protocols.append(f"TLS {detail.get('version', '')}")
                result = {
                    "checked": True,
                    "grade": grade,
                    "protocols": list(set(protocols)),
                    "domain": domain,
                    "warning": (
                        f"SSL Labs grade {grade} — below A. Review TLS configuration."
                        if grade and grade not in ("A", "A+", "A-") else None
                    ),
                }
    except Exception as e:
        logger.warning(f"SSL Labs check failed for {domain}: {e}")

    if result.get("checked"):
        _cache_set(cache_key, result, ttl=43200)  # 12 h
    return result


# ── 4. VirusTotal domain reputation check ────────────────────────────────────
#
# FREE public API — no subscription required.
# Get a free key at https://www.virustotal.com/gui/join-us
# Limits: 4 requests/minute, 500 requests/day (generous for this use case).
#
# Returns: {checked, flagged, malicious_votes, suspicious_votes, reputation, warning}

async def fetch_domain_reputation(vendor_url: str) -> Dict[str, Any]:
    """
    Query VirusTotal's free public API for domain reputation.

    Key fields returned:
      - malicious_votes  : AV vendors that flagged domain as malicious
      - suspicious_votes : AV vendors that flagged as suspicious
      - reputation       : VirusTotal community score (-100 to +100; negative = bad)
      - flagged          : True if malicious_votes > 0 or reputation < -10

    Requires VIRUSTOTAL_API_KEY in environment (free key from virustotal.com).
    Gracefully skipped (checked=False) if key is absent — no crash.
    Cached 24 h.
    """
    from app.core.config import settings

    domain = _domain(vendor_url)
    if not domain:
        return {"checked": False}

    cache_key = f"vt_domain:{domain}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    api_key = getattr(settings, "VIRUSTOTAL_API_KEY", None)
    if not api_key:
        logger.debug("VIRUSTOTAL_API_KEY not configured — domain reputation check skipped")
        return {"checked": False, "skipped_reason": "no_api_key"}

    result: Dict[str, Any] = {"checked": False, "flagged": False}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.virustotal.com/api/v3/domains/{domain}",
                headers={
                    "x-apikey": api_key,
                    "User-Agent": "BooppaBot/1.0",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            attrs = data.get("data", {}).get("attributes", {})
            last_analysis = attrs.get("last_analysis_stats", {})
            malicious = last_analysis.get("malicious", 0)
            suspicious = last_analysis.get("suspicious", 0)
            reputation = attrs.get("reputation", 0)
            categories = attrs.get("categories", {})
            flagged = malicious > 0 or reputation < -10

            warning = None
            if malicious > 0:
                warning = (
                    f"Domain {domain} flagged as malicious by {malicious} security vendor(s) "
                    f"on VirusTotal. This may indicate a security concern."
                )
            elif suspicious > 0:
                warning = (
                    f"Domain {domain} flagged as suspicious by {suspicious} security vendor(s) "
                    f"on VirusTotal."
                )
            elif reputation < -10:
                warning = (
                    f"Domain {domain} has a negative VirusTotal reputation score ({reputation}). "
                    f"This may reflect historical security issues."
                )

            result = {
                "checked": True,
                "flagged": flagged,
                "malicious_votes": malicious,
                "suspicious_votes": suspicious,
                "reputation": reputation,
                "categories": list(set(categories.values()))[:5] if categories else [],
                "domain": domain,
                "warning": warning,
            }
        elif resp.status_code == 404:
            # Domain not in VT database — no known issues
            result = {
                "checked": True,
                "flagged": False,
                "malicious_votes": 0,
                "suspicious_votes": 0,
                "reputation": 0,
                "domain": domain,
                "warning": None,
            }
        elif resp.status_code == 429:
            logger.warning(f"VirusTotal rate limit hit for {domain} — skipping")
        else:
            logger.warning(f"VirusTotal returned HTTP {resp.status_code} for {domain}")
    except Exception as e:
        logger.warning(f"VirusTotal check failed for {domain}: {e}")

    if result.get("checked"):
        _cache_set(cache_key, result, ttl=86400)  # 24 h
    return result


# ── 5. Consistency check ──────────────────────────────────────────────────────

def check_consistency(
    intake: Dict,
    website_text: str,
    pdpc_result: Dict,
    domain_rep: Dict,
) -> list[str]:
    """
    Cross-reference intake declarations against external evidence.
    Returns a list of discrepancy strings (empty = no conflicts found).

    `domain_rep` is the result of fetch_domain_reputation() (VirusTotal).
    """
    import re as _re
    discrepancies = []
    website_lower = website_text.lower() if website_text else ""

    # Audit fix 2: tightened DPO check — require DPO in a contact-like context
    if intake.get("dpo_appointed") == "yes":
        contact_pattern = _re.compile(
            r'(contact|email|reach|enquir|dpo@|officer\s*:).{0,200}'
            r'(data protection officer|dpo|pdpa officer)'
            r'|'
            r'(data protection officer|dpo|pdpa officer).{0,200}'
            r'(contact|email|reach|enquir|@)',
            _re.IGNORECASE,
        )
        if contact_pattern.search(website_lower):
            pass  # DPO found in contact context — good
        elif any(kw in website_lower for kw in ["data protection officer", "dpo@", "pdpa officer"]):
            discrepancies.append(
                "DPO is mentioned on website but not in a clear contact context. "
                "Add DPO name and contact email in your Privacy Policy or Contact page."
            )
        else:
            discrepancies.append(
                "Intake declares DPO appointed, but no DPO reference found on website. "
                "Add DPO contact information to your website to strengthen your submission."
            )

    # DPO email supplied — check if published
    if intake.get("dpo_email") and intake["dpo_email"].lower() not in website_lower:
        discrepancies.append(
            f"DPO email {intake['dpo_email']} provided but not found on website. "
            "Publishing DPO contact details adds credibility."
        )

    # No breach declared but PDPC enforcement exists
    if intake.get("breach_history") == "no" and pdpc_result.get("found"):
        discrepancies.append(
            "Intake declares no data breaches, but PDPC enforcement records found. "
            "Review PDPC enforcement page and update breach_history answer."
        )

    # No breach declared but VirusTotal flagged the domain
    if intake.get("breach_history") == "no" and domain_rep.get("flagged"):
        malicious = domain_rep.get("malicious_votes", 0)
        discrepancies.append(
            f"Intake declares no breaches, but domain flagged by {malicious} VirusTotal vendor(s). "
            "Consider investigating and disclosing if relevant."
        )

    return discrepancies


# ── 6. Hosting signals from HTTP headers ──────────────────────────────────────

async def fetch_hosting_signals(vendor_url: str, stated_hosting: Optional[str] = None) -> Dict[str, Any]:
    """
    Audit fix 3: Infer actual hosting infrastructure from HTTP response headers.
    Checks for CDN/cloud provider signals (AWS, Cloudflare, GCP, Azure, Fastly).
    Returns: {checked, inferred_provider, inferred_region, headers_found, mismatch_warning}
    Cached 12 h.
    """
    domain = _domain(vendor_url)
    if not domain:
        return {"checked": False}

    cache_key = f"hosting_signals:{domain}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # (header_name, required_value_substring_or_None, provider_label)
    HEADER_SIGNALS = [
        ("x-amz-cf-id",      None,       "AWS CloudFront"),
        ("x-amz-request-id", None,       "AWS"),
        ("cf-ray",           None,       "Cloudflare"),
        ("x-served-by",      "fastly",   "Fastly"),
        ("x-goog-hash",      None,       "Google Cloud"),
        ("x-ms-request-id",  None,       "Azure"),
        ("x-azure-ref",      None,       "Azure"),
        ("x-vercel-id",      None,       "Vercel/AWS"),
    ]

    result: Dict[str, Any] = {
        "checked": False,
        "inferred_provider": None,
        "inferred_region": None,
        "headers_found": [],
        "mismatch_warning": None,
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.head(
                vendor_url if vendor_url.startswith("http") else f"https://{domain}",
                headers={"User-Agent": "BooppaBot/1.0"},
            )
        headers_lower = {k.lower(): v.lower() for k, v in resp.headers.items()}
        found_provider = None
        headers_found = []

        for hdr, val_required, provider in HEADER_SIGNALS:
            if hdr in headers_lower:
                if val_required is None or val_required in headers_lower[hdr]:
                    found_provider = provider
                    headers_found.append(hdr)
                    break

        # server header fallback
        if not found_provider:
            server = headers_lower.get("server", "")
            if "cloudflare" in server:
                found_provider = "Cloudflare"
                headers_found.append("server")
            elif "aws" in server or "amazon" in server:
                found_provider = "AWS"
                headers_found.append("server")

        # Try to infer Singapore region
        inferred_region = None
        all_header_vals = " ".join(headers_lower.values())
        if "ap-southeast-1" in all_header_vals or "sin" in headers_lower.get("server", ""):
            inferred_region = "Singapore"

        # Mismatch warning
        mismatch_warning = None
        if stated_hosting and found_provider:
            stated_lower = stated_hosting.lower()
            if "singapore" in stated_lower and not inferred_region:
                mismatch_warning = (
                    f"Intake states data hosted in Singapore, but provider appears to be "
                    f"{found_provider}. Confirm the region is ap-southeast-1."
                )
            elif "on-premise" in stated_lower or "on_premise" in stated_lower:
                mismatch_warning = (
                    f"Intake states on-premise hosting, but site is served via {found_provider}. "
                    "Confirm whether production data is truly on-premise."
                )

        result = {
            "checked": True,
            "inferred_provider": found_provider,
            "inferred_region": inferred_region,
            "headers_found": headers_found,
            "mismatch_warning": mismatch_warning,
        }
    except Exception as e:
        logger.warning(f"Hosting signals check failed for {domain}: {e}")

    if result.get("checked"):
        _cache_set(cache_key, result, ttl=43200)  # 12 h
    return result
