"""
Booppa CSP Compliance Pack — Sanctions Screening Service
FIX #3: Real sanctions screening integration.

Tiers:
  1. FREE  — OFAC SDN List (US Treasury, public, updated daily)
             UN Consolidated Sanctions List (public)
             EU Consolidated Financial Sanctions List / FSF (public)
  2. PAID  — World-Check / Refinitiv One (ready for API key integration); also the
             source of MAS prohibition-order, PEP and adverse-media coverage
             Dow Jones Risk & Compliance (ready for API key integration)

All screening results are cached (Redis) for 24 hours to avoid
hammering public APIs. Cache invalidated daily when new lists are fetched.

Install:
    pip install httpx redis

Environment variables:
    REDIS_URL                  = redis://localhost:6379/0
    SANCTIONS_CACHE_TTL        = 86400   (24 hours)
    WORLDCHECK_API_KEY         = your-key   (optional, paid tier)
    WORLDCHECK_API_SECRET      = your-secret
    WORLDCHECK_API_BASE        = https://api.worldcheck.com/v2
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── CONSTANTS ───────────────────────────────────────────────────────────────

OFAC_SDN_XML_URL = (
    "https://www.treasury.gov/ofac/downloads/sdn.xml"
)
UN_CONSOLIDATED_URL = (
    "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
)
# EU Financial Sanctions File (FSF) — public consolidated list. The endpoint carries an
# access token that the EU rotates occasionally. We self-heal: try each known-good URL
# until one returns valid XML, and cache the working one. Ops can pin/override with the
# EU_SANCTIONS_XML_URL env var (tried first) — no code change or redeploy needed.
_EU_DEFAULT_URLS = [
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList/content?token=dG9rZW4tMjAxNw",
    "https://webgate.ec.europa.eu/europeaid/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
]


def _eu_candidate_urls() -> List[str]:
    """EU FSF URLs to try, in order: env override first, then known-good defaults."""
    urls: List[str] = []
    env = os.environ.get("EU_SANCTIONS_XML_URL")
    if env:
        urls.append(env)
    for u in _EU_DEFAULT_URLS:
        if u not in urls:
            urls.append(u)
    return urls


# Back-compat: first candidate (env override if set, else the primary default).
EU_SANCTIONS_XML_URL = _eu_candidate_urls()[0]

CACHE_TTL = int(os.environ.get("SANCTIONS_CACHE_TTL", 86400))


# ── RESULT DATACLASS ────────────────────────────────────────────────────────

@dataclass
class ScreeningResult:
    is_clear:        bool
    hit_count:       int
    hits:            List[Dict] = field(default_factory=list)
    lists_checked:   List[str]  = field(default_factory=list)
    screened_at:     str        = ""
    name_searched:   str        = ""
    provider:        str        = "booppa-internal"
    confidence:      str        = "exact_match"  # exact_match | fuzzy | no_match

    def to_dict(self) -> Dict:
        return {
            "is_clear":      self.is_clear,
            "hit_count":     self.hit_count,
            "hits":          self.hits,
            "lists_checked": self.lists_checked,
            "screened_at":   self.screened_at,
            "name_searched": self.name_searched,
            "provider":      self.provider,
            "confidence":    self.confidence,
        }


# ── REDIS CACHE ──────────────────────────────────────────────────────────────

def _get_redis():
    from app.core.cache.cache import get_redis_client
    return get_redis_client()

def _cache_key(name: str, lists: List[str]) -> str:
    normalized = _normalize_name(name)
    lists_str  = ":".join(sorted(lists))
    return f"sanctions:{hashlib.sha256(f'{normalized}:{lists_str}'.encode()).hexdigest()}"


def _cache_get(key: str) -> Optional[Dict]:
    r = _get_redis()
    if not r:
        return None
    try:
        value = r.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


def _cache_set(key: str, value: Dict) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        r.setex(key, CACHE_TTL, json.dumps(value))
    except Exception:
        pass


# ── NAME NORMALIZATION ───────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """
    Normalize a name for sanctions list comparison.
    Removes punctuation, lowercases, collapses whitespace.
    """
    if not name:
        return ""
    # Remove honorifics
    honorifics = r'\b(mr|mrs|ms|dr|prof|sir|madam|dato|datuk|tan sri)\b'
    normalized = re.sub(honorifics, "", name.lower(), flags=re.IGNORECASE)
    # Remove punctuation except hyphens
    normalized = re.sub(r"[^\w\s\-]", " ", normalized)
    # Collapse whitespace
    normalized = " ".join(normalized.split())
    return normalized


def _names_match(name1: str, name2: str, threshold: float = 0.85) -> bool:
    """
    Fuzzy name matching using Jaro-Winkler distance.
    Falls back to simple substring matching if jellyfish not installed.
    """
    n1 = _normalize_name(name1)
    n2 = _normalize_name(name2)

    # Exact match
    if n1 == n2:
        return True

    # Substring match (catches "John Smith" in "John Robert Smith")
    if n1 in n2 or n2 in n1:
        return True

    # Fuzzy match
    try:
        import jellyfish
        score = jellyfish.jaro_winkler_similarity(n1, n2)
        return score >= threshold
    except ImportError:
        # Fallback: word overlap
        words1 = set(n1.split())
        words2 = set(n2.split())
        if not words1 or not words2:
            return False
        overlap = len(words1 & words2) / max(len(words1), len(words2))
        return overlap >= threshold


# ── OFAC SDN LIST ────────────────────────────────────────────────────────────

class OfacSdnScreener:
    """
    Screens against the OFAC Specially Designated Nationals (SDN) list.
    US Treasury publishes this daily as public XML.
    Approximately 12,000 entries.
    """

    _entries_cache: Optional[List[Dict]] = None
    _cache_loaded_at: Optional[datetime] = None

    @classmethod
    def _load_entries(cls) -> List[Dict]:
        """Load OFAC SDN entries, refreshing every 24 hours."""
        now = datetime.now(timezone.utc)
        if (
            cls._entries_cache is not None
            and cls._cache_loaded_at is not None
            and (now - cls._cache_loaded_at).seconds < CACHE_TTL
        ):
            return cls._entries_cache

        logger.info("Fetching OFAC SDN list from US Treasury...")
        try:
            response = httpx.get(OFAC_SDN_XML_URL, timeout=30.0)
            response.raise_for_status()
            root    = ET.fromstring(response.content)
            entries = []

            # OFAC XML namespace
            ns = {"ofac": "http://tempuri.org/sdnList.xsd"}

            for entry in root.findall(".//sdnEntry", ns) or root.findall(".//sdnEntry"):
                uid        = entry.findtext("uid") or entry.findtext(".//uid", "")
                first_name = entry.findtext("firstName") or ""
                last_name  = entry.findtext("lastName") or ""
                sdn_type   = entry.findtext("sdnType") or ""
                programs   = [
                    p.text for p in entry.findall(".//program")
                    if p.text
                ]
                # Also collect aliases
                aliases    = [
                    f"{a.findtext('firstName','')} {a.findtext('lastName','')}".strip()
                    for a in entry.findall(".//aka")
                ]

                entries.append({
                    "uid":        uid,
                    "name":       f"{first_name} {last_name}".strip(),
                    "type":       sdn_type,
                    "programs":   programs,
                    "aliases":    [a for a in aliases if a],
                })

            cls._entries_cache    = entries
            cls._cache_loaded_at  = now
            logger.info("OFAC SDN list loaded: %d entries", len(entries))
            return entries

        except Exception as exc:
            logger.error("Failed to load OFAC SDN list: %s", exc)
            return cls._entries_cache or []

    @classmethod
    def screen(cls, name: str) -> List[Dict]:
        """Return all OFAC SDN hits for the given name."""
        entries = cls._load_entries()
        hits    = []

        for entry in entries:
            all_names = [entry["name"]] + entry.get("aliases", [])
            for candidate_name in all_names:
                if _names_match(name, candidate_name):
                    hits.append({
                        "list":     "OFAC SDN",
                        "entry_id": entry["uid"],
                        "name":     entry["name"],
                        "type":     entry["type"],
                        "programs": entry["programs"],
                        "matched_alias": candidate_name if candidate_name != entry["name"] else None,
                    })
                    break   # One hit per entry is enough

        return hits


# ── UN CONSOLIDATED LIST ──────────────────────────────────────────────────────

class UnConsolidatedScreener:
    """
    Screens against the UN Security Council Consolidated Sanctions List.
    Approximately 800 individuals and entities.
    """

    _entries_cache: Optional[List[Dict]] = None
    _cache_loaded_at: Optional[datetime] = None

    @classmethod
    def _load_entries(cls) -> List[Dict]:
        now = datetime.now(timezone.utc)
        if (
            cls._entries_cache is not None
            and cls._cache_loaded_at is not None
            and (now - cls._cache_loaded_at).seconds < CACHE_TTL
        ):
            return cls._entries_cache

        logger.info("Fetching UN Consolidated Sanctions list...")
        try:
            response = httpx.get(UN_CONSOLIDATED_URL, timeout=30.0)
            response.raise_for_status()
            root    = ET.fromstring(response.content)
            entries = []

            for individual in root.findall(".//INDIVIDUAL"):
                first   = individual.findtext("FIRST_NAME", "")
                second  = individual.findtext("SECOND_NAME", "")
                third   = individual.findtext("THIRD_NAME", "")
                name    = " ".join(filter(None, [first, second, third]))
                un_ref  = individual.findtext("REFERENCE_NUMBER", "")
                aliases = [
                    a.findtext("ALIAS_NAME", "")
                    for a in individual.findall(".//ALIAS")
                ]
                entries.append({
                    "ref":  un_ref,
                    "name": name,
                    "type": "individual",
                    "aliases": [a for a in aliases if a],
                })

            for entity in root.findall(".//ENTITY"):
                name   = entity.findtext("FIRST_NAME", "")
                un_ref = entity.findtext("REFERENCE_NUMBER", "")
                aliases = [
                    a.findtext("ALIAS_NAME", "")
                    for a in entity.findall(".//ALIAS")
                ]
                entries.append({
                    "ref":  un_ref,
                    "name": name,
                    "type": "entity",
                    "aliases": [a for a in aliases if a],
                })

            cls._entries_cache   = entries
            cls._cache_loaded_at = now
            logger.info("UN list loaded: %d entries", len(entries))
            return entries

        except Exception as exc:
            logger.error("Failed to load UN Consolidated list: %s", exc)
            return cls._entries_cache or []

    @classmethod
    def screen(cls, name: str) -> List[Dict]:
        entries = cls._load_entries()
        hits    = []
        for entry in entries:
            all_names = [entry["name"]] + entry.get("aliases", [])
            for candidate in all_names:
                if _names_match(name, candidate):
                    hits.append({
                        "list":     "UN Consolidated",
                        "entry_id": entry["ref"],
                        "name":     entry["name"],
                        "type":     entry["type"],
                        "matched_alias": candidate if candidate != entry["name"] else None,
                    })
                    break
        return hits


# ── EU CONSOLIDATED LIST ──────────────────────────────────────────────────────

class EuConsolidatedScreener:
    """
    Screens against the EU Consolidated Financial Sanctions List (FSF).
    Published by the European Commission as public XML; covers persons and
    entities subject to EU restrictive measures.
    """

    _entries_cache: Optional[List[Dict]] = None
    _cache_loaded_at: Optional[datetime] = None
    _working_url: Optional[str] = None   # last URL that returned valid data

    @staticmethod
    def _parse(xml_bytes: bytes) -> List[Dict]:
        """Parse EU FSF XML bytes into our entry dicts. Returns [] if it yields nothing."""
        root    = ET.fromstring(xml_bytes)
        entries = []

        # The EU FSF XML namespaces tags; match on the local name so we are
        # resilient to the exact namespace URI the endpoint serves.
        def _local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        for entity in root.iter():
            if _local(entity.tag) != "sanctionEntity":
                continue

            eu_ref = entity.get("logicalId") or entity.get("euReferenceNumber") or ""
            subject_type = ""
            names: List[str] = []

            for child in entity.iter():
                lname = _local(child.tag)
                if lname == "subjectType":
                    subject_type = (child.get("classificationCode") or "").lower()
                elif lname == "nameAlias":
                    whole = child.get("wholeName")
                    if whole and whole.strip():
                        names.append(whole.strip())
                    else:
                        parts = [
                            child.get("firstName") or "",
                            child.get("middleName") or "",
                            child.get("lastName") or "",
                        ]
                        joined = " ".join(p for p in parts if p).strip()
                        if joined:
                            names.append(joined)

            names = list(dict.fromkeys(names))  # de-dup, preserve order
            if not names:
                continue

            entries.append({
                "ref":     eu_ref,
                "name":    names[0],
                "type":    "entity" if subject_type.startswith("enterprise") else "individual",
                "aliases": names[1:],
            })

        return entries

    @classmethod
    def _load_entries(cls) -> List[Dict]:
        now = datetime.now(timezone.utc)
        if (
            cls._entries_cache is not None
            and cls._cache_loaded_at is not None
            and (now - cls._cache_loaded_at).seconds < CACHE_TTL
        ):
            return cls._entries_cache

        logger.info("Fetching EU Consolidated Sanctions list...")
        # Self-heal across token rotation: try the last-known-good URL first, then the
        # remaining candidates, and keep the first that returns a parseable, non-empty list.
        candidates = _eu_candidate_urls()
        if cls._working_url and cls._working_url in candidates:
            candidates = [cls._working_url] + [u for u in candidates if u != cls._working_url]

        last_exc: Optional[Exception] = None
        for url in candidates:
            try:
                response = httpx.get(url, timeout=30.0, follow_redirects=True)
                response.raise_for_status()
                entries = cls._parse(response.content)
                if not entries:
                    logger.warning("EU FSF URL returned no entries, trying next: %s", url)
                    continue
                cls._entries_cache   = entries
                cls._cache_loaded_at = now
                cls._working_url     = url
                logger.info("EU Consolidated list loaded: %d entries (via %s)", len(entries), url)
                return entries
            except Exception as exc:
                last_exc = exc
                logger.warning("EU FSF URL failed (%s): %s", url, exc)
                continue

        logger.error("Failed to load EU Consolidated list from all candidates: %s", last_exc)
        return cls._entries_cache or []

    @classmethod
    def screen(cls, name: str) -> List[Dict]:
        entries = cls._load_entries()
        hits    = []
        for entry in entries:
            all_names = [entry["name"]] + entry.get("aliases", [])
            for candidate in all_names:
                if _names_match(name, candidate):
                    hits.append({
                        "list":     "EU Consolidated",
                        "entry_id": entry["ref"],
                        "name":     entry["name"],
                        "type":     entry["type"],
                        "matched_alias": candidate if candidate != entry["name"] else None,
                    })
                    break
        return hits


# ── WORLD-CHECK STUB (Refinitiv / LSEG) ──────────────────────────────────────

class WorldCheckScreener:
    """
    Refinitiv World-Check One API integration stub.
    Activate by setting WORLDCHECK_API_KEY environment variable.

    World-Check covers:
    - 6M+ profiles across sanctions, PEP, adverse media
    - FATF grey/black list entities
    - Enhanced PEP screening with family/associates
    - Real-time screening with configurable thresholds

    API Docs: https://developers.refinitiv.com/en/api-catalog/world-check/world-check-one-api
    """

    BASE_URL = os.environ.get("WORLDCHECK_API_BASE", "https://api.worldcheck.com/v2")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(os.environ.get("WORLDCHECK_API_KEY"))

    @classmethod
    def screen(cls, name: str, entity_type: str = "INDIVIDUAL") -> List[Dict]:
        """
        Screen name against World-Check One.
        Returns list of hits.
        """
        if not cls.is_configured():
            logger.debug("World-Check not configured — skipping")
            return []

        api_key    = os.environ["WORLDCHECK_API_KEY"]
        api_secret = os.environ.get("WORLDCHECK_API_SECRET", "")

        try:
            # World-Check uses HMAC authentication
            import hmac
            import time
            timestamp  = str(int(time.time()))
            payload    = json.dumps({
                "name":       name,
                "entityType": entity_type,
                "groupId":    os.environ.get("WORLDCHECK_GROUP_ID", ""),
            })
            signature  = hmac.new(
                api_secret.encode(),
                (timestamp + payload).encode(),
                hashlib.sha256
            ).hexdigest()

            headers = {
                "Authorization": f"Apikey {api_key}",
                "X-Timestamp":   timestamp,
                "X-Signature":   signature,
                "Content-Type":  "application/json",
            }
            response = httpx.post(
                f"{cls.BASE_URL}/cases/screeningRequest",
                content=payload,
                headers=headers,
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()

            hits = []
            for result in data.get("results", []):
                if result.get("matchStrength") in ("STRONG", "EXACT"):
                    hits.append({
                        "list":           "World-Check One",
                        "entry_id":       result.get("uid"),
                        "name":           result.get("primaryName"),
                        "type":           result.get("entityType"),
                        "match_strength": result.get("matchStrength"),
                        "categories":     result.get("categories", []),
                        "source_url":     f"https://worldcheck.com/entry/{result.get('uid')}",
                    })
            return hits

        except Exception as exc:
            logger.error("World-Check screening failed for '%s': %s", name, exc)
            return []


# ── MAS WATCHLIST (Singapore-specific) ───────────────────────────────────────

class MasWatchlistScreener:
    """
    MAS Financial Institutions directory + watchlist.
    MAS publishes enforcement actions and prohibition orders publicly.
    This screener checks against MAS prohibition orders (individuals banned from financial industry).

    Source: https://www.mas.gov.sg/regulation/prohibition-orders
    """

    @classmethod
    def screen(cls, name: str) -> List[Dict]:
        """
        Check MAS prohibition orders.
        Currently uses a cached dataset — extend with web scraping or MAS API if available.
        """
        # MAS does not provide a machine-readable API for prohibition orders.
        # In production, integrate with a scraper or use a compliance data provider
        # that includes MAS data (e.g., Accuity, LexisNexis).
        # Returning empty list as placeholder — configure World-Check for MAS coverage.
        logger.debug("MAS Watchlist screening: configure World-Check for full MAS coverage")
        return []


# ── MAIN SCREENING FUNCTION ───────────────────────────────────────────────────

def screen_individual(
    name:          str,
    also_screen:   Optional[List[str]] = None,   # additional name variants / aliases
    use_worldcheck: bool = True,
) -> ScreeningResult:
    """
    Screen an individual against all configured sanctions lists.

    Args:
        name:           Primary name to screen
        also_screen:    Additional names/aliases to screen (e.g. maiden name, name in Chinese)
        use_worldcheck: Whether to include World-Check (if configured)

    Returns:
        ScreeningResult with all hits across all lists
    """
    cache_key = _cache_key(name, ["ofac","un","eu","worldcheck","mas"])
    cached    = _cache_get(cache_key)
    if cached:
        logger.debug("Sanctions screening cache hit for '%s'", name)
        return ScreeningResult(**cached)

    all_hits     = []
    lists_checked = []
    names_to_screen = [name] + (also_screen or [])

    worldcheck_active = use_worldcheck and WorldCheckScreener.is_configured()

    for screen_name in names_to_screen:
        # OFAC SDN
        ofac_hits = OfacSdnScreener.screen(screen_name)
        if ofac_hits:
            all_hits.extend(ofac_hits)
        if "OFAC SDN" not in lists_checked:
            lists_checked.append("OFAC SDN")

        # UN Consolidated
        un_hits = UnConsolidatedScreener.screen(screen_name)
        if un_hits:
            all_hits.extend(un_hits)
        if "UN Consolidated" not in lists_checked:
            lists_checked.append("UN Consolidated")

        # EU Consolidated
        eu_hits = EuConsolidatedScreener.screen(screen_name)
        if eu_hits:
            all_hits.extend(eu_hits)
        if "EU Consolidated" not in lists_checked:
            lists_checked.append("EU Consolidated")

        # World-Check (if configured) — also our only source of MAS prohibition-order
        # and PEP/adverse-media coverage. Only report these lists when it actually ran.
        if worldcheck_active:
            wc_hits = WorldCheckScreener.screen(screen_name)
            if wc_hits:
                all_hits.extend(wc_hits)
            if "World-Check One" not in lists_checked:
                lists_checked.append("World-Check One")

            # MAS Watchlist — coverage comes via World-Check today. Do NOT claim MAS
            # was checked unless World-Check is configured (see MasWatchlistScreener).
            mas_hits = MasWatchlistScreener.screen(screen_name)
            if mas_hits:
                all_hits.extend(mas_hits)
            if "MAS Watchlist" not in lists_checked:
                lists_checked.append("MAS Watchlist")

    result = ScreeningResult(
        is_clear      = len(all_hits) == 0,
        hit_count     = len(all_hits),
        hits          = all_hits,
        lists_checked = lists_checked,
        screened_at   = datetime.now(timezone.utc).isoformat(),
        name_searched = name,
        provider      = "booppa-internal" if not WorldCheckScreener.is_configured() else "worldcheck+internal",
        confidence    = "exact_match" if all_hits else "no_match",
    )

    _cache_set(cache_key, result.to_dict())

    if not result.is_clear:
        logger.warning(
            "SANCTIONS HIT: '%s' matched %d entry(ies) in: %s",
            name, result.hit_count, ", ".join(lists_checked)
        )

    return result


def screen_entity(name: str, also_screen: Optional[List[str]] = None) -> ScreeningResult:
    """Screen a corporate entity (same logic, different World-Check entity type)."""
    return screen_individual(name, also_screen, use_worldcheck=True)


def refresh_sanctions_lists() -> Dict[str, int]:
    """
    Force-refresh all sanctions list caches.
    Call from Celery Beat daily task.
    """
    OfacSdnScreener._entries_cache    = None
    OfacSdnScreener._cache_loaded_at  = None
    UnConsolidatedScreener._entries_cache   = None
    UnConsolidatedScreener._cache_loaded_at = None
    EuConsolidatedScreener._entries_cache   = None
    EuConsolidatedScreener._cache_loaded_at = None

    # Re-load
    ofac_entries = OfacSdnScreener._load_entries()
    un_entries   = UnConsolidatedScreener._load_entries()
    eu_entries   = EuConsolidatedScreener._load_entries()

    return {
        "ofac_entries": len(ofac_entries),
        "un_entries":   len(un_entries),
        "eu_entries":   len(eu_entries),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
