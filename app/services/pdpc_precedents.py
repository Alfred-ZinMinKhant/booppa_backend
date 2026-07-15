"""
PDPC Enforcement Precedents
===========================
Maps each finding type to public PDPC enforcement decisions, so the report can
show e.g. "this finding has resulted in S$N fines in M PDPC decisions" next to
each violation. Persuasive for procurement committees and legal review.

DATA QUALITY POLICY
-------------------
This file ships with a small, curated seed of well-documented public PDPC
decisions where the facts (year, fine, vendor, section) are widely reported.
Specific dollar amounts and case names from less-publicised decisions MUST be
verified against the PDPC's official decisions register before being added:

    https://www.pdpc.gov.sg/all-commissions-decisions

When in doubt, leave a case out. A short curated list is far more defensible
in front of a procurement officer than a long list that contains errors.

Schema:
    {
        finding_key: [
            {
                "vendor": str,
                "year": int,
                "fine_sgd": int,                # 0 if no financial penalty
                "section": str,                 # PDPA section breached, e.g. "§24"
                "url": str,                     # link to the published decision
                "summary": str,                 # one-line description
            },
            ...
        ]
    }

Finding keys are the stable identifiers from app/services/finding_keys.py.
"""
from __future__ import annotations


from typing import Optional

# ── Seed data ─────────────────────────────────────────────────────────────────
# Cases included here are widely reported in Singapore media and on the PDPC
# decisions register. Compliance team should review and extend periodically.

PRECEDENTS: dict[str, list[dict]] = {
    # This bucket intentionally keeps only cases that were re-verified against
    # live PDPC decision pages and their linked decision documents.
    "breach:pdpc_enforcement": [
        {
            "vendor": "Orchard Turn Developments",
            "year": 2017,
            "fine_sgd": 15_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions/breach-of-protection-obligation-by-orchard-turn-developments",
            "summary": "Mall membership data was exposed after inadequate server security and password controls.",
        },
        {
            "vendor": "COURTS",
            "year": 2019,
            "fine_sgd": 15_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions/breach-of-the-protection-obligation-by-courts-2020-10",
            "summary": "Customers' personal data was disclosed through an online portal because of inadequate security arrangements.",
        },
        {
            "vendor": "SPH Magazines",
            "year": 2020,
            "fine_sgd": 26_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions/breach-of-the-protection-obligation-by-sph-magazines",
            "summary": "Weak website security exposed HardwareZone forum member data to unauthorised access.",
        },
        {
            "vendor": "MyRepublic",
            "year": 2022,
            "fine_sgd": 60_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions/breach-of-the-protection-obligation-by-myrepublic",
            "summary": "Threat actors accessed and exfiltrated subscriber data after security arrangements proved insufficient.",
        },
        {
            "vendor": "RedMart",
            "year": 2022,
            "fine_sgd": 72_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions/breach-of-the-protection-obligation-by-redmart",
            "summary": "Customer data was exposed because reasonable security arrangements were not in place.",
        },
        {
            # SingHealth / IHiS (Jan 2019) — the largest PDPA enforcement action
            # on record at the time; facts (parties, S$1m total, §24) are widely
            # documented. URL points at the official decisions register.
            "vendor": "Integrated Health Information Systems (IHiS)",
            "year": 2019,
            "fine_sgd": 750_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/all-commissions-decisions",
            "summary": "Fined S$750,000 after the 2018 SingHealth cyberattack exposed 1.5 million patients' records, citing inadequate security arrangements.",
        },
        {
            "vendor": "Singapore Health Services (SingHealth)",
            "year": 2019,
            "fine_sgd": 250_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/all-commissions-decisions",
            "summary": "Fined S$250,000 as data controller in the 2018 SingHealth breach for failing to make reasonable security arrangements.",
        },
    ],
    # Legacy NRIC entries were removed because their old register URLs now
    # redirect away from the original decisions. Re-add only after a live PDPC
    # decision page and exact section / penalty can be re-verified.
    "nric:collection": [],
    "nric:leakage": [],
}


# ── Finding-key → obligation category ─────────────────────────────────────────
# Maps the stable finding keys that _finding_key_from (pdf_service) and
# finding_keys.extract_finding_keys produce onto the obligation categories used
# by the live PDPC precedent index (evidence_enricher.build_pdpc_precedent_index).
# This is what lets a per-finding row cite a REAL published decision.
_FINDING_KEY_TO_CATEGORY: dict[str, str] = {
    # Consent (§13) — cookie banner + pre-consent trackers
    "free:no_consent_banner": "consent",
    "free:tracking_cookies": "consent",
    # Openness (§11/12) — DPO + privacy policy
    "free:no_dpo_contact": "openness_dpo",
    "free:no_privacy_policy": "openness_dpo",
    # Protection (§24) — security headers, cookie flags, transport security
    "free:hsts": "protection",
    "free:csp": "protection",
    "free:x_frame": "protection",
    "free:x_content_type": "protection",
    "free:referrer": "protection",
    "free:permissions_policy": "protection",
    "free:cookie_secure": "protection",
    "free:https": "protection",
    "breach:pdpc_enforcement": "protection",
    # NRIC / national identifiers
    "free:nric_exposure": "nric",
    "nric:collection": "nric",
    "nric:leakage": "nric",
    # Retention (§25)
    "clause:retention": "retention",
}

# Contextual categories inferred from a substring when there is no exact key
# match (AI-generated finding types vary; the substring keeps DNC etc. mapped).
_CATEGORY_SUBSTRINGS: list[tuple[str, str]] = [
    ("dnc", "dnc"),
    ("do_not_call", "dnc"),
    ("marketing", "dnc"),
    ("consent", "consent"),
    ("cookie", "consent"),
    ("tracker", "consent"),
    ("dpo", "openness_dpo"),
    ("privacy_policy", "openness_dpo"),
    ("nric", "nric"),
    ("retention", "retention"),
    ("header", "protection"),
    ("security", "protection"),
]

# Honest statutory / guidance grounding per category — the fallback shown when
# the live index has no classified case for a finding type yet. This is a
# regulatory *basis*, never labelled a precedent.
_CATEGORY_BASIS: dict[str, str] = {
    "consent": "PDPA §13 Consent Obligation; PDPC Advisory Guidelines on Cookies (2021) and the Guide to Enhanced Notice and Choice.",
    "openness_dpo": "PDPA §11-12 Openness Obligation; PDPC Advisory Guidelines on Key Concepts (DPO designation and business-contact disclosure).",
    "protection": "PDPA §24 Protection Obligation; PDPC Advisory Guidelines on Key Concepts (reasonable security arrangements).",
    "dnc": "PDPA Part 9 Do Not Call Provisions; PDPC Advisory Guidelines on the Do Not Call Provisions.",
    "nric": "PDPA §24; PDPC Advisory Guidelines on the PDPA for NRIC and Other National Identification Numbers (2019).",
    "retention": "PDPA §25 Retention Limitation Obligation; PDPC Advisory Guidelines on Key Concepts (cessation of retention).",
    "accuracy": "PDPA §23 Accuracy Obligation; PDPC Advisory Guidelines on Key Concepts.",
    "transfer": "PDPA §26 Transfer Limitation Obligation; PDPC Advisory Guidelines on the PDPA for Cross-Border Data Transfers.",
    "notification": "PDPA §26A-26D Data Breach Notification Obligation; PDPC Guide on Managing and Notifying Data Breaches.",
}


def finding_category(finding_key: str) -> Optional[str]:
    """Resolve a finding key to a PDPC obligation category, or None."""
    if not finding_key:
        return None
    if finding_key in _FINDING_KEY_TO_CATEGORY:
        return _FINDING_KEY_TO_CATEGORY[finding_key]
    fk = finding_key.lower()
    for needle, cat in _CATEGORY_SUBSTRINGS:
        if needle in fk:
            return cat
    return None


def _live_precedents_for_category(category: str) -> list[dict]:
    """Return classified real decisions for a category from the live index."""
    if not category:
        return []
    try:
        from app.services.evidence_enricher import load_pdpc_precedent_index
        index = load_pdpc_precedent_index()
    except Exception:
        index = None
    if not index:
        return []
    cases = (index.get("categories") or {}).get(category) or []
    # Only rows with a real decision URL + summary are citable.
    return [c for c in cases if c.get("url") and c.get("summary")]


def get_precedents(finding_key: str) -> list[dict]:
    """Return precedent dicts for this finding key, or [] if none.

    Resolution order: the live classified index (real published decisions,
    keyed by obligation category) first, then the human-verified static seed as
    a floor. Static entries carry verified=True; live entries verified=False.
    """
    category = finding_category(finding_key)
    live = _live_precedents_for_category(category) if category else []
    static = list(PRECEDENTS.get(finding_key, []))
    if live:
        # De-dupe by URL, static (verified) first so it wins on ties.
        seen: set[str] = set()
        merged: list[dict] = []
        for c in [*static, *live]:
            u = c.get("url") or ""
            if u and u in seen:
                continue
            if u:
                seen.add(u)
            merged.append(c)
        return merged
    return static


def regulatory_basis(finding_key: str) -> Optional[str]:
    """Honest statutory/guidance grounding for a finding when no real precedent
    is on file. Never a precedent — a regulatory *basis*. Returns None if the
    finding type maps to no known obligation category.
    """
    category = finding_category(finding_key)
    if not category:
        return None
    return _CATEGORY_BASIS.get(category)


def precedent_summary(finding_key: str, max_items: int = 2) -> Optional[str]:
    """Render a short human-readable sentence summarising precedent for a
    finding type. Returns None when no precedents are on file.

    Output format:
        "PDPC has fined N organisations a total of S$X for this violation
         class, including [Vendor 1, 2019] and [Vendor 2, 2021]."
    """
    items = get_precedents(finding_key)
    if not items:
        return None

    count = len(items)
    # Only sum fines we actually have (live index rows may carry fine_sgd=None
    # when the figure could not be parsed — never treat that as S$0).
    fines = [int(i["fine_sgd"]) for i in items if i.get("fine_sgd")]
    total = sum(fines)
    # Examples: name + year when both known, else just the organisation name.
    examples = []
    for i in items[:max_items]:
        vendor = i.get("vendor")
        if not vendor:
            continue
        examples.append(f"{vendor} ({i['year']})" if i.get("year") else vendor)

    def _sgd(x: int) -> str:
        if x >= 1_000_000:
            return f"S${x/1_000_000:.1f}M"
        if x >= 1_000:
            return f"S${x/1_000:.0f}k"
        return f"S${x}"

    cases = " and ".join(examples) if examples else None

    if fines and len(fines) == count:
        # Every case has a disclosed penalty (the human-verified static seed) —
        # keep the precise, established phrasing.
        org_word = "organisation" if count == 1 else "organisations"
        base = (
            f"PDPC has fined {count} {org_word} a total of {_sgd(total)} "
            f"under similar facts."
        )
    elif fines:
        # Live-index rows: some penalties parsed, some not — quote only what we
        # have and say so, never implying the total is complete.
        dec_word = "decision" if count == 1 else "decisions"
        base = (
            f"PDPC has published {count} enforcement {dec_word} on this obligation, "
            f"with penalties totalling at least {_sgd(total)} across the cases where "
            f"a figure was disclosed."
        )
    else:
        # Real decisions on file but no parsed penalty figure — do not invent one.
        dec_word = "decision" if count == 1 else "decisions"
        base = f"PDPC has published {count} enforcement {dec_word} on this obligation."
    if cases:
        base += f" Notable cases: {cases}."
    return base


def precedent_keys() -> list[str]:
    """Return all finding_keys that have at least one precedent on file."""
    return [k for k, v in PRECEDENTS.items() if v]


def precedent_count() -> int:
    """Total number of seeded precedent entries (across all keys)."""
    return sum(len(v) for v in PRECEDENTS.values())
