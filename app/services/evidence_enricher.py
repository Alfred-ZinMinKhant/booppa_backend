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


import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date
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


async def _match_acra(
    db, uen: Optional[str], company: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Pure two-step ACRA match — writes nothing.

    Returns `(registered_name, resolved_uen)` for the given UEN/company subject:
    an exact `DiscoveredVendor` UEN lookup first, then a live data.gov.sg fuzzy
    match on the company name. `fetch_acra_status` is Redis-cached 24h.
    """
    registered_name: Optional[str] = None
    resolved_uen: Optional[str] = uen

    if uen:
        try:
            from app.core.models import DiscoveredVendor

            dv = db.query(DiscoveredVendor).filter(DiscoveredVendor.uen == uen).first()
            if dv and dv.company_name:
                registered_name = dv.company_name
        except Exception as exc:
            logger.warning("_match_acra: DiscoveredVendor lookup failed for %s: %s", uen, exc)

    if not registered_name and (uen or company):
        try:
            live = await fetch_acra_status(uen, company_name=company)
            if live.get("found"):
                if not resolved_uen and live.get("uen"):
                    resolved_uen = live.get("uen")
                if live.get("registered_name"):
                    registered_name = live.get("registered_name")
        except Exception as exc:
            logger.warning("_match_acra: live ACRA lookup failed for %s/%s: %s", uen, company, exc)

    return registered_name, resolved_uen


def _norm(s) -> str:
    """Case/space-insensitive normalization for comparing company hint strings."""
    return (s or "").strip().lower()


def _norm_domain(s) -> str:
    """Normalize a website/domain for comparison: drop scheme, `www.`, path and
    trailing slash, lowercase. `https://WWW.Acme.com/about` -> `acme.com`."""
    v = (s or "").strip().lower()
    if not v:
        return ""
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    return v.rstrip(".")


def _cache_trusted_for_hint(user, company_hint, website_hint=None) -> bool:
    """Whether the cached `User.legal_name`/`User.uen` may be reused for a render
    scoped to `company_hint` (and, when given, `website_hint`).

    Company trust: no hint given (an own-account render), the hint is the account's
    own `company`, or the hint matches the provenance recorded in
    `User.legal_name_hint` (i.e. the cache was resolved from this same hint). A
    divergent hint means the cache belongs to a *different* entity/purchase and must
    be re-resolved fresh — this is what stops a reused account from stamping a
    previously cached entity (the sticky "SPQR Communications" leak).

    Website trust: a `website_hint` that matches neither the account's own `website`
    nor the `User.website_hint` provenance breaks trust **on its own**, even when the
    company string matches. Trading names collide, and several deliverables render the
    domain as the assessed subject, so a different site is a different subject.
    """
    site = _norm_domain(website_hint)
    if site:
        known_sites = {
            _norm_domain(getattr(user, "website", None)),
            _norm_domain(getattr(user, "website_hint", None)),
        }
        known_sites.discard("")
        if known_sites and site not in known_sites:
            return False

    hint = _norm(company_hint)
    if not hint:
        return True
    if hint == _norm(getattr(user, "legal_name_hint", None)):
        return True
    # A cached identity with NO recorded provenance predates the provenance write and
    # cannot be vouched for — even when the hint equals the account's own `company`.
    # This is the reported failure: company="netpoleons" with a cached
    # legal_name/uen belonging to SPQR Communications and legal_name_hint=NULL. The
    # company match would wave it through, so provenance is checked first; distrust
    # forces a fresh resolution, which then writes correct provenance and self-heals.
    _has_cache = bool(
        _norm(getattr(user, "legal_name", None)) or _norm(getattr(user, "uen", None))
    )
    _has_provenance = bool(_norm(getattr(user, "legal_name_hint", None)))
    if _has_cache and not _has_provenance:
        return False
    if hint == _norm(getattr(user, "company", None)):
        return True
    # Unclaimed account: nothing cached and no company on file, so there is no other
    # entity's identity to leak and the first resolution legitimately claims it. Note
    # a cached `legal_name` with NULL provenance does NOT qualify — that is precisely
    # the pre-fix signature we must distrust.
    if not any(
        _norm(getattr(user, f, None))
        for f in ("company", "legal_name", "legal_name_hint")
    ):
        return True
    return False


def _subject_is_own_account(user, company_hint, website_hint=None) -> bool:
    """Whether the subject being resolved *is* this account's own entity — i.e.
    whether a freshly resolved identity may be persisted onto the account.

    Deliberately distinct from `_cache_trusted_for_hint`, which answers the *read*
    question ("may I reuse what's cached?"). A pre-fix row with a poisoned cache and
    no provenance fails the read check but must still pass this one: the account
    genuinely is that company, so resolving its own name afresh is exactly what
    repairs the row. Conflating the two would leave poisoned accounts unable to heal.
    """
    site = _norm_domain(website_hint)
    if site:
        known_sites = {
            _norm_domain(getattr(user, "website", None)),
            _norm_domain(getattr(user, "website_hint", None)),
        }
        known_sites.discard("")
        if known_sites and site not in known_sites:
            return False

    hint = _norm(company_hint)
    if not hint:
        return True
    if hint == _norm(getattr(user, "company", None)):
        return True
    if hint == _norm(getattr(user, "legal_name_hint", None)):
        return True
    # Unclaimed account — no company on file and nothing cached to overwrite.
    return not any(
        _norm(getattr(user, f, None))
        for f in ("company", "legal_name", "legal_name_hint")
    )


def trusted_cached_uen(user, company_hint=None, website_hint=None) -> Optional[str]:
    """The account's cached `User.uen`, but **only** when it may be trusted for this
    subject — otherwise `None`.

    This is the read-side gate. Callers must treat `None` as "no UEN supplied" and
    fall through to a name-only ACRA lookup rather than reaching for `user.uen`
    directly. Reading the raw column is how a stale UEN (SPQR's `21374700J`) got
    matched against the registry and certified as another company's identity.
    """
    if not _cache_trusted_for_hint(user, company_hint, website_hint=website_hint):
        logger.info(
            "trusted_cached_uen: refusing cached UEN for user %s (company_hint=%r, "
            "website_hint=%r) — cache provenance does not match this subject",
            getattr(user, "id", None), company_hint, website_hint,
        )
        return None
    return getattr(user, "uen", None)


def clear_cached_identity(user, db, *, commit: bool = True) -> None:
    """Drop the account's cached identity **and its provenance**.

    Use when the account's company/website changes or a resolution fails — leaving
    `legal_name_hint` behind while clearing `legal_name` produces a row that looks
    like it has trustworthy provenance for a value that no longer exists.
    """
    user.legal_name = None
    user.uen = None
    user.legal_name_hint = None
    user.website_hint = None
    if commit:
        db.commit()


def record_verified_identity(
    user,
    db,
    *,
    uen: Optional[str] = None,
    legal_name: Optional[str] = None,
    company_hint: Optional[str] = None,
    website_hint: Optional[str] = None,
) -> bool:
    """The **only** sanctioned way to persist `User.uen` / `User.legal_name`.

    Nothing outside this module may assign those columns directly (there is a
    regression test asserting exactly that). A direct write is how a Vendor Proof
    run for one company stamped another company's ACRA registration onto the account
    and then re-confirmed it on every subsequent run.

    Writes only when `_cache_trusted_for_hint` says this subject *is* the account's
    own entity, and always records the provenance (`legal_name_hint` /
    `website_hint`) alongside — a write without provenance is what let the previous
    corruption survive the next trust check.

    Returns True when something was persisted.
    """
    if not _subject_is_own_account(user, company_hint, website_hint=website_hint):
        logger.warning(
            "record_verified_identity: refused identity write for user %s — subject "
            "(company_hint=%r, website_hint=%r) is not this account's own entity; "
            "rejected uen=%r legal_name=%r (cached: company=%r legal_name_hint=%r)",
            getattr(user, "id", None), company_hint, website_hint, uen, legal_name,
            getattr(user, "company", None), getattr(user, "legal_name_hint", None),
        )
        return False

    updated = False
    if uen and uen != getattr(user, "uen", None):
        user.uen = uen
        updated = True
    if legal_name and legal_name != getattr(user, "legal_name", None):
        user.legal_name = legal_name
        updated = True
    # Provenance: record what this cache was resolved from so a later purchase with a
    # different company/website can detect it is stale (see _cache_trusted_for_hint).
    if (uen or legal_name) and company_hint:
        if getattr(user, "legal_name_hint", None) != company_hint:
            user.legal_name_hint = company_hint
            updated = True
    if (uen or legal_name) and website_hint:
        if getattr(user, "website_hint", None) != website_hint:
            user.website_hint = website_hint
            updated = True

    if updated:
        db.commit()
    return updated


async def resolve_legal_name(
    user,
    db,
    company_hint: Optional[str] = None,
    uen: Optional[str] = None,
    website_hint: Optional[str] = None,
) -> str:
    """
    Resolve a user's ACRA-registered legal entity name and persist it to
    `User.legal_name`, recovering `User.uen` along the way if it was missing.

    Same two-step match as the inline logic this replaces (formerly duplicated
    in app/services/fulfillment/single_products.py): an exact `DiscoveredVendor`
    UEN lookup first, then a live data.gov.sg fuzzy match on company name. Safe
    to call repeatedly — `fetch_acra_status` is cached 24h and this function
    only writes to the DB when the resolved name actually changes.

    Returns the resolved legal name, or the best available fallback
    (`user.legal_name` -> `company_hint`/`user.company` -> "Your Organisation")
    if no registry match is found.
    """
    # Only use the cached UEN if the cache is trusted for this company/website context
    trusted = _cache_trusted_for_hint(user, company_hint, website_hint=website_hint)
    _uen = uen
    if trusted:
        _uen = _uen or getattr(user, "uen", None)
    _company = company_hint or getattr(user, "company", None)

    registered_name, resolved_uen = await _match_acra(db, _uen, _company)

    # Only persist the resolution back to the account when the subject we resolved
    # actually corresponds to *this user's own* entity. A hint derived from some
    # other entity (e.g. a vendor being verified) must never overwrite the
    # account's cached identity — that is exactly how a stray value like
    # "SPQR Communications" became sticky and contaminated later reports. When the
    # incoming hint diverges from the cached identity we still return the freshly
    # resolved name to the caller, but we do not write it to User.
    #
    # The trust decision and the write both live in record_verified_identity, the
    # single sanctioned write path — this function must not assign the columns itself.
    if not _subject_is_own_account(user, _company, website_hint=website_hint):
        # Cross-entity subject: never write to the account, and never fall back to the
        # account's cached legal_name — that belongs to a *different* entity and is
        # exactly the leak we are killing. Scope strictly to the resolved-from-hint
        # value, then the raw hint.
        return registered_name or _company or "Your Organisation"

    record_verified_identity(
        user,
        db,
        uen=resolved_uen,
        legal_name=registered_name,
        company_hint=_company,
        website_hint=website_hint,
    )

    return registered_name or getattr(user, "legal_name", None) or _company or "Your Organisation"


# Outer deadline for an on-demand ACRA resolution performed while a customer's
# document is being rendered. `fetch_acra_status` walks the deduped dataset IDs
# from `_acra_dataset_ids()` sharing one `httpx.AsyncClient(timeout=10)`, so a
# full walk can take ~30s. We deliberately do NOT budget for the full walk: 15s
# covers the primary dataset lookup plus the fuzzy match, and the legacy-dataset
# fallback is best-effort enrichment that must not hold a paid PDF open. Steady
# state is ~0 either way — results are Redis-cached for 24h.
ACRA_SYNC_RESOLVE_TIMEOUT = 15


def _legal_name_fallback(user) -> str:
    """Shared fallback order for a user's entity name: resolved legal name ->
    raw company field -> a neutral placeholder. Never falls back to the Booppa
    platform name."""
    return (
        getattr(user, "legal_name", None)
        or getattr(user, "company", None)
        or "Your Organisation"
    )


def _display_fallback(user, company_hint, trusted: bool) -> str:
    """Fallback entity name when resolution didn't yield a fresh value.

    For a trusted (own-account) render, fall back to the account's cached
    identity as before. For a cross-entity hint we must NOT surface the cached
    `legal_name` (it belongs to a different entity); prefer the purchase's own
    hint, then a neutral placeholder."""
    if trusted:
        return _legal_name_fallback(user)
    return (company_hint or "").strip() or "Your Organisation"


async def resolve_display_legal_name(
    user,
    db=None,
    timeout: int = ACRA_SYNC_RESOLVE_TIMEOUT,
    company_hint: str | None = None,
    website_hint: str | None = None,
) -> str:
    """Async-context version of `display_legal_name` — use this from any `async
    def`.

    `display_legal_name` backfills a missing legal name via `asyncio.run()`,
    which raises inside a running loop. Every Celery fulfillment workflow runs
    under `asyncio.run(...)`, so those callers MUST await this instead or the
    ACRA backfill silently never happens.

    Pass `company_hint` to scope the render to a *specific purchase's* company
    rather than the account's cached identity. When the hint diverges from what
    the cache was resolved from, the cached `legal_name` is treated as stale and
    re-resolved fresh (and never written back to the account).
    """
    trusted = _cache_trusted_for_hint(user, company_hint, website_hint=website_hint)
    if db is not None and (not getattr(user, "legal_name", None) or not trusted):
        try:
            resolved = await asyncio.wait_for(
                resolve_legal_name(
                    user,
                    db,
                    company_hint=company_hint or getattr(user, "company", None),
                    website_hint=website_hint,
                ),
                timeout=timeout,
            )
            if resolved:
                return resolved
        except asyncio.TimeoutError:
            logger.warning(
                "resolve_display_legal_name: ACRA resolution timed out after %ss for user %s",
                timeout, getattr(user, "id", None),
            )
        except Exception as e:
            logger.warning("Failed to resolve legal name: %s", e)

    # resolve_legal_name mutates and commits `user` for own-account hints, so the
    # fallback re-reads the attribute; for cross-entity hints it prefers the hint.
    return _display_fallback(user, company_hint, trusted)


async def resolve_report_legal_name(
    report, db=None, timeout: int = ACRA_SYNC_RESOLVE_TIMEOUT
) -> str:
    """Resolve the ACRA-registered legal name for the SUBJECT OF A REPORT.

    Scoped strictly to the report's own subject (`company_name` / `company_website`
    and the UEN carried in `assessment_data["uen"]`). NEVER reads or writes
    `User.legal_name` / `User.uen` — a certified deliverable must reflect the entity
    it was purchased to verify, not whatever identity is cached on the buyer's
    account. This is the fix for the "wrong company certified" class of bug where a
    reused account's stale `legal_name` (e.g. "SPQR Communications") leaked into a
    report about a different vendor.

    Returns the resolved registered name, falling back to the report's own
    `company_name`, then "Your Organisation". Caches the resolution back onto the
    report (`company_name` + `assessment_data["resolved_uen"]`) so re-renders stay
    stable and scoped to this report.
    """
    subject_name = (getattr(report, "company_name", None) or "").strip() or None
    assessment = report.assessment_data if isinstance(getattr(report, "assessment_data", None), dict) else {}
    subject_uen = (
        (assessment.get("uen") or getattr(report, "uen", None) or "").strip() or None
        if (assessment.get("uen") or getattr(report, "uen", None))
        else None
    )

    if db is None or (not subject_name and not subject_uen):
        return subject_name or "Your Organisation"

    try:
        registered_name, resolved_uen = await asyncio.wait_for(
            _match_acra(db, subject_uen, subject_name), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "resolve_report_legal_name: ACRA resolution timed out after %ss for report %s",
            timeout, getattr(report, "id", None),
        )
        return subject_name or "Your Organisation"
    except Exception as e:
        logger.warning("resolve_report_legal_name: resolution failed: %s", e)
        return subject_name or "Your Organisation"

    # Cache the scoped resolution onto the report (never onto User).
    if registered_name and registered_name != subject_name:
        try:
            report.company_name = registered_name
            if resolved_uen and isinstance(getattr(report, "assessment_data", None), dict):
                report.assessment_data = {**report.assessment_data, "resolved_uen": resolved_uen}
            db.commit()
        except Exception as exc:
            logger.warning("resolve_report_legal_name: report cache write failed: %s", exc)
            db.rollback()

    return registered_name or subject_name or "Your Organisation"


def _run_coro_blocking(coro, timeout: int):
    """Drive `coro` to completion from synchronous code, with or without a loop
    already running on this thread.

    `asyncio.run()` raises inside a running loop, and every Celery fulfillment
    workflow runs under `asyncio.run(...)`. The previous behaviour was to detect
    that case and skip resolution entirely — which made "skip the ACRA lookup" the
    *normal* production path, so a correctly-distrusted identity cache silently
    degraded to the raw hint instead of being re-resolved. That is why a stale
    cached identity kept surfacing on documents.

    When a loop is already running we hand the coroutine to a short-lived worker
    thread with its own loop and block on the result. The calling thread is parked
    for the duration, so the `db` Session is still touched by exactly one thread at
    a time (Sessions are not concurrency-safe, but sequential hand-off is fine).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        )
        # Slightly beyond the inner deadline so the inner wait_for is what fires,
        # giving us the proper TimeoutError rather than a bare thread timeout.
        return future.result(timeout=timeout + 5)


def display_legal_name(
    user,
    db=None,
    timeout: int = ACRA_SYNC_RESOLVE_TIMEOUT,
    company_hint: str | None = None,
    website_hint: str | None = None,
) -> str:
    """Sync entry point for rendering a user's entity name.

    If legal_name is missing (or stale for `company_hint`) and db is provided,
    actively resolves the ACRA name. Safe to call from a sync function whether or
    not an event loop happens to be running above it (see `_run_coro_blocking`).
    From an `async def`, prefer `await resolve_display_legal_name(...)` — it does
    the same work without the worker-thread hop.

    Pass `company_hint` to scope the render to a *specific purchase's* company
    rather than the account's cached identity; a divergent hint treats the cache
    as stale and re-resolves fresh (never written back to the account).
    """
    trusted = _cache_trusted_for_hint(user, company_hint, website_hint=website_hint)
    if db is not None and (not getattr(user, "legal_name", None) or not trusted):
        try:
            # Hard-capped total wait: under a stalled/blackholed network the
            # per-request httpx timeouts don't always bound total wall time
            # (e.g. hung DNS). A Celery task must never hang indefinitely here.
            resolved = _run_coro_blocking(
                resolve_legal_name(
                    user,
                    db,
                    company_hint=company_hint or getattr(user, "company", None),
                    website_hint=website_hint,
                ),
                timeout=timeout,
            )
            if resolved:
                return resolved
        except asyncio.TimeoutError:
            logger.warning(
                "display_legal_name: ACRA resolution timed out after %ss for user %s",
                timeout, getattr(user, "id", None),
            )
        except Exception as e:
            logger.warning("Failed to sync-resolve legal name: %s", e)

    return _display_fallback(user, company_hint, trusted)


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

# Bumped whenever row semantics change, because the index is cached for 14 days
# and a stale index would otherwise outlive the code fix.
#   v2 — v1 indexed "No Breach of …" decisions as enforcement (see
#        _PDPC_NON_ENFORCEMENT_RE).
#   v3 — rows gained `year_source`; without it a year renders unlabelled, and a
#        publication year would read as the year the decision was made.
PDPC_PRECEDENT_INDEX_CACHE_KEY = "pdpc_precedent_index:v3"

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

# Decisions whose outcome was NOT an enforcement finding. These titles carry the
# same obligation words as real breaches, so they must be excluded on outcome
# before the obligation rules run — a cleared organisation must never appear in
# the precedent index.
_PDPC_NON_ENFORCEMENT_RE = re.compile(
    r"\bno\s+(further\s+action|breach|financial\s+penalty|contravention)"
    r"|\bnot\s+in\s+breach"
    r"|\bdid\s+not\s+(breach|contravene)"
    r"|\bno\s+directions?\s+(issued|given)",
    re.IGNORECASE,
)

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
    never guessed into a bucket), and for decisions whose OUTCOME was not an
    enforcement finding: "No Breach of the Protection Obligation by X" matches
    the same obligation regex as "Breach of …", so without the outcome filter a
    cleared organisation gets indexed and later cited as a precedent against it.
    """
    t = title or ""
    if _PDPC_NON_ENFORCEMENT_RE.search(t):
        return []
    cats: list[str] = []
    for pattern, cat in _PDPC_TITLE_RULES:
        if re.search(pattern, t, flags=re.IGNORECASE) and cat not in cats:
            cats.append(cat)
    return cats


_VENDOR_NOISE_RE = re.compile(
    r"\b(pte|private|ltd|limited|llp|inc|corp|corporation|company|co)\b|[^\w\s]",
    re.IGNORECASE,
)


def precedent_dedupe_key(case: dict) -> tuple:
    """Identity of a precedent for de-duplication.

    The register publishes some decisions under two URLs (e.g. PPLingo 2024,
    Tanah Merah Country Club), so de-duping on URL alone double-counts them —
    which inflates both the case count and the penalty total we quote. The same
    key also collapses a curated seed case against its scraped twin.
    """
    vendor = _VENDOR_NOISE_RE.sub(" ", (case.get("vendor") or "")).lower()
    return (" ".join(vendor.split()), case.get("fine_sgd"))


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
# PDPC decisions carry a neutral citation like "[2023] SGPDPC 4" (or SGPDPCR) —
# that bracketed year IS the decision year and is the only reliable signal.
# A bare "\b20\d{2}\b" is NOT usable: the first four-digit 20xx on a decision
# page is almost always boilerplate (an address, a section reference, a fine
# figure) — that produced the impossible "(2000)" year on every case.
_YEAR_RE = re.compile(r"\[(20\d{2})\]\s*SGPDPC", re.IGNORECASE)
# Fallback: the decision's own publication date, read from the page banner
# ("Published on 18 Feb 2022"). Anchored to the "Published on" label so it cannot
# drift onto a stray 20xx elsewhere on the page — that anchoring is the whole
# point of the rule above. The neutral citation is still preferred when present,
# because publication can trail the decision date across a year boundary.
_PUBLISHED_YEAR_RE = re.compile(
    r"Published on[\s\"'\\,:\[\]-]*(?:<!--\s*-->)?[\s\"'\\,]*"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+(20\d{2})",
    re.IGNORECASE,
)


def _parse_decision_year(url: str, text: str) -> "int | None":
    """Return the PDPC decision year, or None when it can't be confidently read.

    Two signals are trusted, in order: the neutral-citation year
    (``[YYYY] SGPDPC…``), then the page's own "Published on <date>" banner. The
    citation wins because publication can trail the decision across a year
    boundary; the banner is the fallback because most register summary pages
    carry no citation at all (it lives in the linked decision PDF). Both are
    labelled facts read off the page — an unanchored ``20\\d{2}`` match is still
    never used, since the first one on a page is usually an address or a section
    reference. Anything else yields None and the precedent summary names the case
    without a year, which is honest, rather than stamping a fabricated one.

    The year must fall in the enforcement era (PDPA came into force in 2013).
    """
    return _parse_decision_year_with_source(url, text)[0]


def _parse_decision_year_with_source(url: str, text: str) -> "tuple[int | None, str | None]":
    """As _parse_decision_year, plus WHICH signal the year came from.

    Returns ``(year, "decided")`` for a neutral-citation year and
    ``(year, "published")`` for the register's publication banner. The caller
    labels the year with this in customer-facing text, so a publication year is
    never passed off as the year the decision was made.
    """
    m = _YEAR_RE.search(url) or _YEAR_RE.search(text)
    source = "decided"
    if not m:
        m = _PUBLISHED_YEAR_RE.search(text)
        source = "published"
    if not m:
        return None, None
    year = int(m.group(1))
    if not (2013 <= year <= date.today().year + 1):
        return None, None
    return year, source


async def _enrich_decision_page(client: "httpx.AsyncClient", url: str) -> dict:
    """Best-effort: fetch a single decision page and parse fine + year.

    Returns {fine_sgd, year} with None for anything not confidently parsed. Any
    network/parse failure yields both None — we never fabricate a figure.
    """
    out: dict[str, Any] = {"fine_sgd": None, "year": None, "year_source": None}
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
        out["year"], out["year_source"] = _parse_decision_year_with_source(url, text)
    except Exception:
        pass
    return out


async def build_pdpc_precedent_index(max_enrich: int = 500) -> Dict[str, Any]:
    """Scrape the PDPC decisions list and build a per-obligation precedent index.

    Shape written to cache under PDPC_PRECEDENT_INDEX_CACHE_KEY:
        {
          "built_at": <iso>,
          "categories": { "<category>": [ {vendor, year, fine_sgd, section,
                                           url, summary, verified}, ... ] },
          "total": <int>,
          "citable": <int>,
        }

    Every classified decision page is fetched, because `verified` — the flag that
    decides whether a case may be NAMED in a customer document — is earned from
    the page, not the title: a row is verified only when we parsed BOTH a
    financial penalty and a "[YYYY] SGPDPC N" citation year. The penalty is what
    proves the decision was an enforcement finding at all; a cleared organisation
    has none, so evidence (not the title blocklist) is the real gate against
    citing an acquittal. Title-only rows stay in the index with verified=False —
    useful as context, never citable.

    `max_enrich` is a safety ceiling on pages fetched per run, not a budget to
    spend: rows beyond it stay unverified rather than being cited unchecked.
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

    # Evidence pass: read every classified decision page (bounded concurrency so
    # we stay a polite scraper). A page that fails to fetch or parse simply
    # leaves fine/year as None — that row is then not citable, never guessed.
    if classified:
        sem = asyncio.Semaphore(5)

        async def _enrich(row: dict, client) -> None:
            async with sem:
                enr = await _enrich_decision_page(client, row["url"])
            row["fine_sgd"] = enr["fine_sgd"]
            row["year"] = enr["year"]
            row["year_source"] = enr.get("year_source")

        try:
            async with httpx.AsyncClient(timeout=12) as client:
                await asyncio.gather(*(
                    _enrich(row, client) for row in classified[:max_enrich]
                ))
        except Exception as e:
            logger.warning(f"PDPC precedent index: enrichment pass failed: {e}")

    categories: dict[str, list[dict]] = {}
    seen_per_cat: dict[str, set] = {}
    seen_global: set = set()
    for row in classified:
        # Verified == citable by name: penalty proves an enforcement outcome,
        # citation year proves we read the actual decision.
        row_verified = bool(row.get("fine_sgd")) and bool(row.get("year"))
        if row_verified:
            seen_global.add(precedent_dedupe_key(row))
        for cat in row["categories"]:
            # Collapse the same decision published under two URLs.
            if row_verified:
                key = precedent_dedupe_key(row)
                if key in seen_per_cat.setdefault(cat, set()):
                    continue
                seen_per_cat[cat].add(key)
            categories.setdefault(cat, []).append({
                "vendor": row.get("vendor"),
                "year": row.get("year"),
                # "decided" (neutral citation) or "published" (register banner)
                "year_source": row.get("year_source"),
                "fine_sgd": row.get("fine_sgd"),
                "section": _CATEGORY_SECTION.get(cat, ""),
                "url": row["url"],
                "summary": row["title"],
                "verified": row_verified,
            })

    index = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories,
        "citable": len(seen_global),
        "total": len(classified),
    }
    _cache_set(PDPC_PRECEDENT_INDEX_CACHE_KEY, index, ttl=14 * 86400)  # 14 days
    logger.info(
        "PDPC precedent index built: %d decisions across %d categories "
        "(%d citable — penalty + citation year read from the decision page)",
        len(classified), len(categories), len(seen_global),
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
