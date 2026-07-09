"""Sector-priority ordering for the 13 MAS TRM domains.

A MAS supervisor begins a TRM review with the domains most material to the
entity's sector. A fintech assessment that leads with Technology Risk Governance,
Cyber Security and Data Management signals a sector-specific posture; one that
lists the domains in flat numerical order looks like a generic template
(forensic-audit finding). This reorders the controls so the sector-critical
domains come first, and exposes the critical set so callers can tag them.

Pure functions — no DB, no I/O — so they're cheap to unit test and safe to call
from both the baseline generator and the workspace API.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from app.core.models import MAS_TRM_DOMAINS

# Exact MAS_TRM_DOMAINS names, ordered by criticality per sector. Only domains
# that should be surfaced first need listing; the remainder keep canonical order.
SECTOR_PRIORITY_DOMAINS: dict[str, list[str]] = {
    "fintech": [
        "Technology Risk Governance",
        "Cyber Security",
        "Data and Information Management",
        "Authentication and Access Management",
        "IT Outsourcing and Vendor Management",
    ],
    "healthcare": [
        "Data and Information Management",
        "Incident Management",
        "Business Continuity and Disaster Recovery",
        "Cyber Security",
        "Authentication and Access Management",
    ],
}

# Free-text sector inputs (User.industry / intake) → canonical sector key.
_SECTOR_ALIASES: dict[str, str] = {
    "fintech": "fintech",
    "finance": "fintech",
    "financial": "fintech",
    "financial services": "fintech",
    "banking": "fintech",
    "bank": "fintech",
    "payments": "fintech",
    "insurance": "fintech",
    "insurtech": "fintech",
    "healthcare": "healthcare",
    "health": "healthcare",
    "healthtech": "healthcare",
    "medical": "healthcare",
    "medtech": "healthcare",
    "pharma": "healthcare",
    "pharmaceutical": "healthcare",
}

_CANONICAL_INDEX = {d: i for i, d in enumerate(MAS_TRM_DOMAINS)}


def normalise_sector(sector: str | None) -> str | None:
    """Map a free-text sector to a known key ('fintech'/'healthcare'), or None."""
    if not sector:
        return None
    key = str(sector).strip().lower()
    if key in SECTOR_PRIORITY_DOMAINS:
        return key
    return _SECTOR_ALIASES.get(key)


def critical_domains(sector: str | None) -> list[str]:
    """The sector-critical MAS TRM domains (empty list if sector unknown)."""
    norm = normalise_sector(sector)
    return list(SECTOR_PRIORITY_DOMAINS.get(norm, [])) if norm else []


def _default_get_domain(c: Any) -> str | None:
    if isinstance(c, dict):
        return c.get("domain")
    return getattr(c, "domain", None)


def reorder_controls_by_sector(
    controls: Iterable[Any],
    sector: str | None,
    get_domain: Callable[[Any], str | None] | None = None,
) -> list[Any]:
    """Return `controls` reordered so the sector-critical domains lead.

    Works on TrmControl ORM rows or plain dicts (anything exposing `domain`).
    Critical domains come first in their priority order; everything else follows
    in canonical MAS_TRM_DOMAINS order. Unknown/empty sector → canonical order.
    """
    getter = get_domain or _default_get_domain
    items = list(controls)
    pri = critical_domains(sector)
    if not pri:
        return sorted(items, key=lambda c: _CANONICAL_INDEX.get(getter(c), 99))
    rank = {d: i for i, d in enumerate(pri)}
    return sorted(
        items,
        key=lambda c: (
            rank.get(getter(c), len(pri)),               # critical domains first
            _CANONICAL_INDEX.get(getter(c), 99),         # then canonical order
        ),
    )
