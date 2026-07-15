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
import json
import logging
import re
from typing import Any, Dict, List, Optional
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

# data.gov.sg dataset IDs, tried in order. The first comes from config so it can
# be repointed without a code change; the rest are legacy fallbacks kept for
# resilience if the primary dataset is retired.
def _acra_dataset_ids() -> list[str]:
    from app.core.config import settings
    primary = getattr(settings, "ACRA_DATASET_ID", "d_3f960c10fed6145404ca7b821f263b87")
    legacy = [
        "d_3f960c10fed6145404ca7b821f263b87",
        "d_82ce0e3a0ce059e0a7b36c43e4cd5c96",
        "5ab68aac-91f6-4f39-9b21-698610bdf3f7",
    ]
    # de-dup while preserving order, primary first
    seen: set[str] = set()
    return [d for d in [primary, *legacy] if not (d in seen or seen.add(d))]


# Non-live UEN status vocabulary across ACRA entity kinds (companies vs
# sole-props/businesses use different words). Anything not clearly live is
# treated as inactive so a struck-off/ceased entity never reads as verified.
_ACRA_DEAD_STATUS_TOKENS = (
    "DEREGISTER", "STRUCK", "CANCELLED", "CANCELED", "CEASED",
    "DISSOLVED", "EXPIRED", "WOUND UP", "TERMINATED", "WITHDRAWN",
)


def _acra_is_live(status: str) -> bool:
    """Interpret a raw ACRA UEN-status string as active/live.

    Live entities read as "Registered" (companies) or "Live" (sole-props /
    businesses). Any dead-status token wins over an ambiguous match.
    """
    s = (status or "").upper()
    if not s:
        return False
    if any(tok in s for tok in _ACRA_DEAD_STATUS_TOKENS):
        return False
    return "LIVE" in s or "REGISTERED" in s or s == "REGISTERED"


def _acra_field(rec: Dict[str, Any], *names: str) -> str:
    """First non-empty value among the given field names (dataset schemas vary)."""
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


# ACRA entity names are all-caps with a legal-form suffix ("PTE. LTD.", "LLP",
# "PRIVATE LIMITED"). Strip the suffix and punctuation before comparing so a
# buyer typing "SINGTEL SOMERSET" still matches "SINGTEL SOMERSET PTE. LTD.".
_ACRA_LEGAL_SUFFIXES = (
    "PRIVATE LIMITED", "PTE. LTD.", "PTE LTD", "PTE. LTD", "PTE LTD.",
    "LIMITED LIABILITY PARTNERSHIP", "LLP", "LLC", "LTD.", "LTD",
    "& CO.", "& CO", "& COMPANY", "CORPORATION", "CORP.", "CORP", "INC.", "INC",
)


def _acra_normalize_name(name: str) -> str:
    """Uppercase, strip legal-form suffixes and punctuation, collapse whitespace."""
    if not name:
        return ""
    s = name.upper()
    s = re.sub(r"[^\w\s&]", " ", s)          # drop punctuation (keep & for "& CO")
    s = " ".join(s.split())
    # Strip a trailing legal suffix (longest first so "PTE. LTD." wins over "LTD").
    for suf in _ACRA_LEGAL_SUFFIXES:
        suf_norm = " ".join(re.sub(r"[^\w\s&]", " ", suf.upper()).split())
        if suf_norm and s.endswith(" " + suf_norm):
            s = s[: -(len(suf_norm) + 1)].rstrip()
            break
        if s == suf_norm:
            break
    return " ".join(s.split())


def _acra_best_match(query: str, records: List[Dict[str, Any]], threshold: float = 0.90):
    """Pick the record whose entity_name best matches ``query`` via Jaro-Winkler.

    Returns ``(record, score)`` for the best record at/above ``threshold``, else
    ``(None, best_score)``. Never blindly returns records[0] — a ``q=`` search
    returns any record containing the term, most of which are the wrong company.
    """
    qn = _acra_normalize_name(query)
    if not qn:
        return None, 0.0
    try:
        import jellyfish
        scorer = jellyfish.jaro_winkler_similarity
    except ImportError:
        scorer = None
    best_rec, best_score = None, 0.0
    for rec in records:
        cand = _acra_normalize_name(_acra_field(rec, "entity_name", "company_name"))
        if not cand:
            continue
        if cand == qn or qn in cand or cand in qn:
            score = 1.0
        elif scorer is not None:
            score = scorer(qn, cand)
        else:
            w1, w2 = set(qn.split()), set(cand.split())
            score = len(w1 & w2) / max(len(w1), len(w2)) if w1 and w2 else 0.0
        if score > best_score:
            best_rec, best_score = rec, score
    if best_score >= threshold:
        return best_rec, best_score
    return None, best_score


async def fetch_acra_status(uen: Optional[str] = None, company_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Query data.gov.sg ACRA dataset for live entity status.
    Returns: {found, live, entity_status, entity_type, registered_name, registration_date, warning}
    Cached 24 h.
    """
    if not uen and not company_name:
        return {"found": False}

    # v2 cache namespace — v1 entries were written under a broken field mapping
    # that returned empty status/type and false live=False; do not let them mask
    # the corrected reads.
    cache_key = f"acra_live:v2:{uen.upper() if uen else company_name.lower().replace(' ', '_')}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    result: Dict[str, Any] = {"found": False}
    headers = {"User-Agent": "BooppaBot/1.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        for dataset_id in _acra_dataset_ids():
            try:
                # UEN lookup is an exact filter → 1 record. Name lookup uses a
                # per-field full-text search scoped to entity_name, which is far
                # more precise than a free q= (that OR-matches every token across
                # all columns and buries the real entity). Still pull a handful
                # of candidates and fuzzy-verify below.
                params = {"resource_id": dataset_id, "limit": 1 if uen else 30}
                if uen:
                    params["filters"] = f'{{"uen":"{uen.upper()}"}}'
                elif company_name:
                    # Query on the distinctive core name only — the legal suffix
                    # ("PTE LTD" etc.) appears in nearly every entity and would
                    # dilute the field-scoped search.
                    core = _acra_normalize_name(company_name) or company_name
                    params["q"] = json.dumps({"entity_name": core})

                resp = await client.get(
                    "https://data.gov.sg/api/action/datastore_search",
                    params=params,
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                records = data.get("result", {}).get("records", [])
                if not records:
                    continue

                if uen:
                    rec = records[0]
                else:
                    # Name-only: verify the returned name actually matches the
                    # buyer's company before declaring a registry hit. A q= term
                    # like "SINGTEL" matches dozens of unrelated entities.
                    rec, score = _acra_best_match(company_name or "", records)
                    if rec is None:
                        logger.info(
                            "ACRA name '%s' had no candidate >= 0.90 (best %.2f) in %s",
                            company_name, score, dataset_id,
                        )
                        continue
                # Normalise field names — the current dataset uses *_desc suffixes
                # (uen_status_desc / entity_type_desc / uen_issue_date); legacy
                # datasets used *_description. Read every known variant.
                entity_status = _acra_field(
                    rec, "uen_status_desc", "entity_status_description",
                    "uen_status_description", "status",
                ).upper()
                live = _acra_is_live(entity_status)
                result = {
                    "found": True,
                    "live": live,
                    # Echo back the registry UEN — when the caller searched by
                    # company name only, this is how they recover the official
                    # UEN to display / persist on the certificate.
                    "uen": _acra_field(rec, "uen"),
                    "entity_status": entity_status,
                    "entity_type": _acra_field(
                        rec, "entity_type_desc", "entity_type_description", "entity_type",
                    ),
                    "registered_name": _acra_field(rec, "entity_name", "company_name"),
                    "registration_date": _acra_field(
                        rec, "uen_issue_date", "incorporation_date", "registration_date",
                    ),
                    "warning": None if live else f"ACRA status: {entity_status} — company may not be active",
                }
                break
            except Exception as e:
                logger.warning(f"ACRA dataset {dataset_id} query failed: {e}")

    if not result["found"]:
        result["warning"] = f"Entity {uen or company_name} not found in ACRA dataset — data may be stale or name incorrect"

    _cache_set(cache_key, result, ttl=86400)  # 24 h
    return result


# ── 2. PDPC enforcement check ─────────────────────────────────────────────────

# The old `/all-enforcement-decisions` slug now 404s. The live listing is the
# "All Commission's Decisions" page, which server-embeds the full decision list
# as a JSON island (an Angular app) rather than as <a> tags.
PDPC_ENFORCEMENT_URL = "https://www.pdpc.gov.sg/all-commissions-decisions"

# Each decision is emitted in the page's embedded JSON as
#   {"label":"Breach of ... by <Org>","url":"/organisations/.../enforcement-decisions/<slug>","isListingDetail":true}
# The quotes are backslash-escaped in the raw HTML, so `\\?"` tolerates both the
# escaped island form and a plain-JSON form.
_PDPC_DECISION_RE = re.compile(
    r'\\?"label\\?"\s*:\s*\\?"(?P<title>[^"\\]{6,250})\\?"'
    r'\s*,\s*\\?"url\\?"\s*:\s*\\?"(?P<url>/[^"\\]*enforcement-decisions/[^"\\]+?)\\?"'
)


def _extract_pdpc_decisions(html: str) -> list[tuple[str, str]]:
    """Return [(title, absolute_url), ...] of PDPC decisions from the listing HTML.

    Primary path parses the embedded JSON island (the current site structure);
    falls back to <a> tags if the page ever reverts to server-rendered anchors.
    De-duped by URL, order preserved.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def _add(title: str, href: str) -> None:
        title = (title or "").strip()
        if not title or len(title) < 12:
            return
        url = href if href.startswith("http") else f"https://www.pdpc.gov.sg{href}"
        if url in seen:
            return
        seen.add(url)
        out.append((title, url))

    for m in _PDPC_DECISION_RE.finditer(html or ""):
        _add(m.group("title"), m.group("url"))

    if not out:  # fallback: legacy anchor-tag structure
        try:
            from bs4 import BeautifulSoup
            for a in BeautifulSoup(html or "", "lxml").find_all("a", href=True):
                href = a.get("href", "")
                if "enforcement-decisions/" in href:
                    _add(a.get_text(strip=True), href)
        except ImportError:
            pass

    return out


async def fetch_pdpc_enforcement(company_name: str, uen: Optional[str] = None) -> Dict[str, Any]:
    """
    Scrape PDPC enforcement decisions list and check if vendor appears.
    Returns: {checked, found, cases: [{title, date, url}], warning}
    Cached 6 h (page doesn't change often but we want same-day freshness).
    """
    name_lower = company_name.lower().strip()
    uen_upper = uen.upper() if uen else None

    # First, check static curated list
    from app.services.pdpc_precedents import PRECEDENTS
    static_cases = []
    for prec in PRECEDENTS.get("breach:pdpc_enforcement", []):
        vendor_lower = prec.get("vendor", "").lower()
        if vendor_lower and (vendor_lower in name_lower or name_lower in vendor_lower):
            static_cases.append({
                "title": f"PDPC Decision ({prec.get('year')}): {prec.get('summary')} (Fine: S${prec.get('fine_sgd')})",
                "date": str(prec.get("year", "")),
                "url": prec.get("url", PDPC_ENFORCEMENT_URL)
            })

    if static_cases:
        return {
            "checked": True,
            "found": True,
            "cases": static_cases,
            "warning": f"PDPC enforcement action found for {company_name}. This should be disclosed in RFP submissions."
        }

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
            # Do not return checked=False, to avoid the 'unavailable' warning for all companies
            return {"checked": True, "found": False, "cases": []}

    if not page_cache:
        return {"checked": True, "found": False, "cases": []}

    html = page_cache.get("html", "")
    decisions = _extract_pdpc_decisions(html)

    if not decisions:
        # Structure changed and nothing parsed — fall back to a raw substring
        # check so a genuine match isn't silently missed.
        if name_lower in html.lower():
            return {
                "checked": True,
                "found": True,
                "cases": [{"title": f"Reference to {company_name} found on PDPC enforcement page", "date": "", "url": PDPC_ENFORCEMENT_URL}],
                "warning": f"Possible PDPC enforcement action found for {company_name}. Manual review recommended.",
            }
        return {"checked": True, "found": False, "cases": []}

    found_cases = []

    for text, url in decisions:
        text_lower = text.lower()
        if name_lower in text_lower or (uen_upper and uen_upper in text.upper()):
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


# ── 2b. Live PDPC precedent index (classified by obligation) ──────────────────
# The vendor-match scrape above fetches the ENTIRE decisions list and keeps only
# rows matching the scanned vendor. This builder reuses the same list to produce
# a per-obligation index so each finding type can cite REAL published decisions —
# the "PDPC enforcement precedents per finding" feature — instead of a static seed.
#
# Honesty rules baked in:
#   • A row only becomes a citable precedent if it has a real title + decision URL.
#   • fine_sgd / year are populated only when parsed from the decision page; when
#     they cannot be parsed they stay None and are never printed as a number.
#   • verified=False marks machine-classified rows (vs the human-verified static
#     seed in pdpc_precedents.PRECEDENTS, which carries verified=True).

PDPC_PRECEDENT_INDEX_CACHE_KEY = "pdpc_precedent_index:v1"

# Obligation categories keyed off the standard, formulaic PDPC decision titles.
# Order does not matter — every matching category is attached (a title may cite
# more than one obligation, e.g. "Protection and Openness Obligations"). Matched
# case-insensitively. The Protection rule excludes "Data Protection Officer" (a
# DPO/Openness matter) via a negative look-behind so it does not double-tag.
_PDPC_TITLE_RULES: list[tuple[str, str]] = [
    (r"(?<!data )protection\b(?!\s+officer)", "protection"),
    (r"\bconsent\b", "consent"),
    (r"openness|data protection officer|\bDPO\b|failure to (appoint|make available)", "openness_dpo"),
    (r"do not call|\bDNC\b|telemarketing|marketing message|specified message|unsolicited", "dnc"),
    (r"\bNRIC\b|national registration identity|identity card number", "nric"),
    (r"retention", "retention"),
    (r"\baccuracy\b", "accuracy"),
    (r"transfer limitation|transfer obligation|cross-border", "transfer"),
    (r"notification obligation|data breach", "notification"),
]

# Section label shown next to each category when we cite it.
_CATEGORY_SECTION: dict[str, str] = {
    "protection": "§24 Protection Obligation",
    "consent": "§13 Consent Obligation",
    "openness_dpo": "§11/§12 Openness Obligation (DPO)",
    "dnc": "Do Not Call Provisions (Part 9)",
    "nric": "NRIC / national identifiers (Advisory Guidelines)",
    "retention": "§25 Retention Limitation Obligation",
    "accuracy": "§23 Accuracy Obligation",
    "transfer": "§26 Transfer Limitation Obligation",
    "notification": "§26A-D Data Breach Notification Obligation",
}


def classify_pdpc_title(title: str) -> list[str]:
    """Return the obligation categories a PDPC decision title implicates.

    Title text on the decisions register is formulaic ("Breach of the Protection
    Obligation by X"), so a small regex table classifies reliably. Returns [] for
    titles we cannot confidently place (they are then left out of the index —
    never guessed into a bucket).
    """
    t = title or ""
    cats: list[str] = []
    for pattern, cat in _PDPC_TITLE_RULES:
        if re.search(pattern, t, flags=re.IGNORECASE) and cat not in cats:
            cats.append(cat)
    return cats


def _vendor_from_title(title: str) -> Optional[str]:
    """Extract the organisation name from '... Obligation(s) by [Org]' titles."""
    m = re.search(r"\bby\s+(.+?)\s*$", (title or "").strip(), flags=re.IGNORECASE)
    if not m:
        return None
    vendor = m.group(1).strip(" .")
    # Trim trailing decision-reference noise like "[2023] SGPDPC 4".
    vendor = re.sub(r"\[\d{4}\].*$", "", vendor).strip(" .,")
    return vendor or None


_FINE_RE = re.compile(r"financial penalty of\s*S?\$?\s*([\d,]+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


async def _enrich_decision_page(client: "httpx.AsyncClient", url: str) -> dict:
    """Best-effort: fetch a single decision page and parse fine + year.

    Returns {fine_sgd, year} with None for anything not confidently parsed. Any
    network/parse failure yields both None — we never fabricate a figure.
    """
    out: dict[str, Any] = {"fine_sgd": None, "year": None}
    try:
        resp = await client.get(url, headers={"User-Agent": "BooppaBot/1.0"}, follow_redirects=True)
        if resp.status_code != 200:
            return out
        text = resp.text
        fm = _FINE_RE.search(text)
        if fm:
            try:
                out["fine_sgd"] = int(fm.group(1).replace(",", ""))
            except ValueError:
                pass
        ym = _YEAR_RE.search(url) or _YEAR_RE.search(text)
        if ym:
            out["year"] = int(ym.group(1))
    except Exception:
        pass
    return out


async def build_pdpc_precedent_index(max_enrich: int = 60) -> Dict[str, Any]:
    """Scrape the PDPC decisions list and build a per-obligation precedent index.

    Shape written to cache under PDPC_PRECEDENT_INDEX_CACHE_KEY:
        {
          "built_at": <iso>,
          "categories": { "<category>": [ {vendor, year, fine_sgd, section,
                                           url, summary, verified}, ... ] },
          "total": <int>,
        }
    `max_enrich` caps how many decision pages we fetch for fine/year (keeps the
    beat task bounded); the rest are indexed title-only with fine/year=None.
    """
    from datetime import datetime, timezone

    # Reuse the cached list HTML if the vendor-match path already fetched it.
    cache_key = "pdpc_enforcement_list"
    page_cache = _cache_get(cache_key)
    html = page_cache.get("html", "") if page_cache else ""
    if not html:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    PDPC_ENFORCEMENT_URL,
                    headers={"User-Agent": "BooppaBot/1.0"},
                    follow_redirects=True,
                )
            if resp.status_code == 200:
                html = resp.text
                _cache_set(cache_key, {"html": html}, ttl=21600)
        except Exception as e:
            logger.warning(f"PDPC precedent index: list fetch failed: {e}")
            return {"built_at": None, "categories": {}, "total": 0, "error": str(e)}

    decisions = _extract_pdpc_decisions(html)
    if not decisions:
        logger.warning("PDPC precedent index: no decisions parsed from listing")
        return {"built_at": None, "categories": {}, "total": 0, "error": "no decisions parsed"}

    # `_extract_pdpc_decisions` already de-dupes by URL; classify each.
    classified: list[dict] = []
    for title, url in decisions:
        cats = classify_pdpc_title(title)
        if not cats:
            continue
        classified.append({
            "title": title,
            "url": url,
            "categories": cats,
            "vendor": _vendor_from_title(title),
        })

    # Best-effort enrichment (bounded).
    if classified:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                for row in classified[:max_enrich]:
                    enr = await _enrich_decision_page(client, row["url"])
                    row["fine_sgd"] = enr["fine_sgd"]
                    row["year"] = enr["year"]
        except Exception as e:
            logger.warning(f"PDPC precedent index: enrichment pass failed: {e}")

    categories: dict[str, list[dict]] = {}
    for row in classified:
        for cat in row["categories"]:
            categories.setdefault(cat, []).append({
                "vendor": row.get("vendor"),
                "year": row.get("year"),
                "fine_sgd": row.get("fine_sgd"),
                "section": _CATEGORY_SECTION.get(cat, ""),
                "url": row["url"],
                "summary": row["title"],
                "verified": False,
            })

    index = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories,
        "total": len(classified),
    }
    _cache_set(PDPC_PRECEDENT_INDEX_CACHE_KEY, index, ttl=14 * 86400)  # 14 days
    logger.info(
        "PDPC precedent index built: %d decisions across %d categories",
        len(classified), len(categories),
    )
    return index


def load_pdpc_precedent_index() -> Optional[Dict[str, Any]]:
    """Return the cached classified index, or None if it has not been built."""
    return _cache_get(PDPC_PRECEDENT_INDEX_CACHE_KEY)


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


def extract_website_signals(website_text: str, privacy_policy_text: Optional[str] = None) -> Dict[str, Any]:
    """Extract structured compliance signals from the buyer's website + privacy
    policy text. Powers two things:

    1. AI prompt — these signals become "Verified from website:" facts so the
       LLM can confidently name what's published instead of inventing.
    2. Per-answer verification source attribution on the result page + PDF.

    Returns dict with: iso_27001 / soc_2 / encryption / aws / azure / gcp /
    singapore_residency / dpa / sub_processors flags, plus any extracted year
    or specific terms. All checks are case-insensitive and forgiving on
    spacing/hyphenation since vendor sites vary.
    """
    import re as _re

    blob = ((website_text or "") + "\n" + (privacy_policy_text or "")).lower()
    if not blob.strip():
        return {"available": False}

    def has(pattern: str) -> bool:
        return bool(_re.search(pattern, blob, _re.IGNORECASE))

    def extract(pattern: str) -> str | None:
        m = _re.search(pattern, blob, _re.IGNORECASE)
        return m.group(1) if m else None

    # ── Certifications ──────────────────────────────────────────────────────
    iso27001 = has(r"\biso[\s\-/]?2[\s\-/]?7[\s\-/]?0[\s\-/]?0[\s\-/]?1\b")
    iso27001_year = extract(r"iso[\s\-/]?27001[\s:/-]*(\d{4})")
    iso27017 = has(r"\biso[\s\-/]?27017\b")
    iso27018 = has(r"\biso[\s\-/]?27018\b")
    iso27701 = has(r"\biso[\s\-/]?27701\b")
    soc2 = has(r"\bsoc[\s\-]?2\b|\bsoc[\s\-]?ii\b")
    pci_dss = has(r"\bpci[\s\-]?dss\b")
    gdpr = has(r"\bgdpr\b|\bgeneral data protection regulation\b")
    pdpa_mention = has(r"\bpdpa\b|\bpersonal data protection act\b")

    # ── Encryption ──────────────────────────────────────────────────────────
    aes_mentioned = has(r"\baes[\s\-]?(?:128|192|256)\b")
    tls_mentioned = has(r"\btls[\s]?1\.?[023]\b|\btransport layer security\b")
    encryption_generic = has(r"\bencryption\b|\bencrypted\b|\bencrypt\b")

    # ── Cloud providers ────────────────────────────────────────────────────
    aws = has(r"\baws\b|\bamazon web services\b")
    azure = has(r"\bmicrosoft azure\b|\bazure cloud\b|(?<!\w)azure(?=[^a-z])")
    gcp = has(r"\bgcp\b|\bgoogle cloud\b")
    oci = has(r"\boracle cloud\b|\boci\b")

    # ── Data residency ──────────────────────────────────────────────────────
    singapore_residency = has(
        r"\bdata\s+(?:centers?|centres?)\s+in\s+singapore\b"
        r"|\bhosted\s+in\s+singapore\b"
        r"|\bstored\s+in\s+singapore\b"
        r"|\bsingapore\s+region\b"
        r"|\bap[-\s]?southeast[-\s]?1\b"
    )
    non_sg_regions: list[str] = []
    for pat, label in [
        (r"\bus[-\s]?east[-\s]?[12]\b", "AWS us-east"),
        (r"\bus[-\s]?west[-\s]?[12]\b", "AWS us-west"),
        (r"\beu[-\s]?(?:west|central)[-\s]?\d\b", "AWS eu region"),
        (r"\bcalifornia\b.{0,40}\bdata\s+center\b", "California data center"),
    ]:
        if has(pat):
            non_sg_regions.append(label)

    # ── Privacy policy contents ─────────────────────────────────────────────
    has_dpa = has(r"\bdata\s+processing\s+agreement\b|\bdpa\b")
    has_subprocessors = has(
        r"\bsub[-\s]?processors?\b|\bsub[-\s]?contractors?\b"
        r"|\bthird[-\s]?party\s+processors?\b"
    )
    has_dpo = has(r"\bdata\s+protection\s+officer\b|\bdpo\b")
    has_breach_policy = has(
        r"\b(?:data\s+)?breach\s+(?:notification|response|policy)\b"
        r"|\bincident\s+response\b"
    )
    has_retention_policy = has(r"\bretention\s+(?:policy|period|schedule)\b")

    return {
        "available": True,
        # Certifications — these are the most valuable signals because they
        # ground the most-often-fabricated claims.
        "iso_27001_mentioned": iso27001,
        "iso_27001_year": iso27001_year,
        "iso_27017_mentioned": iso27017,
        "iso_27018_mentioned": iso27018,
        "iso_27701_mentioned": iso27701,
        "soc_2_mentioned": soc2,
        "pci_dss_mentioned": pci_dss,
        "gdpr_mentioned": gdpr,
        "pdpa_mentioned": pdpa_mention,
        # Encryption
        "aes_mentioned": aes_mentioned,
        "tls_mentioned": tls_mentioned,
        "encryption_generic": encryption_generic,
        # Cloud / hosting
        "aws_mentioned": aws,
        "azure_mentioned": azure,
        "gcp_mentioned": gcp,
        "oci_mentioned": oci,
        # Residency
        "singapore_residency_mentioned": singapore_residency,
        "non_sg_regions_mentioned": non_sg_regions,
        # Policy completeness
        "dpa_mentioned": has_dpa,
        "subprocessors_mentioned": has_subprocessors,
        "dpo_mentioned": has_dpo,
        "breach_policy_mentioned": has_breach_policy,
        "retention_policy_mentioned": has_retention_policy,
    }


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
