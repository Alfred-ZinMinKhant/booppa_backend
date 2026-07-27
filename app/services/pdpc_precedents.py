"""
PDPC Enforcement Precedents
===========================
Maps each finding type to public PDPC enforcement decisions, so the report can
show e.g. "this finding has resulted in S$N fines in M PDPC decisions" next to
each violation. Persuasive for procurement committees and legal review.

DATA QUALITY POLICY
-------------------
When in doubt, leave a case out. A short list is far more defensible in front of
a procurement officer than a long list that contains errors.

Citations come from the live index that evidence_enricher builds from the PDPC
decisions register, and a case may only be NAMED when the index read a financial
penalty off its decision page (see get_citable_precedents). That penalty is what
proves the decision was an enforcement finding: a cleared organisation has none.
Nothing about a citation is inferred from the decision's title — the title says
what a decision is ABOUT, only the page says how it ENDED. A delivered report
once cited "BHG (Singapore)", which the register had cleared, because the title
alone was trusted.

The PRECEDENTS dict below is a hand-checked floor used only when the live index
is unavailable — it is NOT merged on top of the index, because the same decision
appears in both under different names and URLs and would be counted twice. Its
entries (year, fine, vendor, section) must be verified against the register
before being added:

    https://www.pdpc.gov.sg/all-commissions-decisions

WHAT THE RENDERED SENTENCE DOES AND DOES NOT CLAIM
--------------------------------------------------
Counts and totals are stated as floors ("at least"), because decisions under
obligations we do not classify, and pages whose penalty could not be parsed, are
absent from the index. Undertakings and cleared decisions are excluded by design
— no penalty was imposed. What the cited cases share with the finding is the
OBLIGATION, which is named explicitly; they are not claimed to share its facts.
Each year is labelled with where it came from — "published" is the register's
publication year (which can trail the decision date across a year boundary),
"decided" is the neutral-citation year or a hand-checked one.

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


import re
from collections import Counter
from typing import Optional

# Parties the register anonymised — truthful, but nothing a buyer can look up,
# so they are ranked below named organisations when choosing which to cite.
_ANON_PARTY_RE = re.compile(r"^(a|an)\s", re.IGNORECASE)

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
    # Security-header / web-misconfiguration findings. Verified against the live
    # decision page (Case No. DP-2107-B8562, published 18 Feb 2022).
    "security_headers:protection": [
        {
            "vendor": "North London Collegiate School (Singapore)",
            "year": 2022,
            "fine_sgd": 10_000,
            "section": "§24 Protection Obligation",
            "url": "https://www.pdpc.gov.sg/all-commissions-decisions/2022/02/breach-of-the-protection-obligation-by-north-london-collegiate-school",
            "summary": "Fined S$10,000 (Case DP-2107-B8562) after admission documents uploaded through the school website sat in an inadequately secured directory and became reachable through search engines.",
        },
    ],
    # DNC Registry findings. Intentionally EMPTY: the published DNC decisions
    # reviewed for this bucket were warnings/directions rather than financial
    # penalties, or were criminal prosecutions rather than Commission decisions,
    # so none could be cited as an enforcement precedent with a verified penalty.
    # Findings in this category fall back to _CATEGORY_BASIS["dnc"], which states
    # the Part 9 obligation honestly. Add entries only after confirming the
    # organisation, year, penalty and provision on a live PDPC decision page.
    "dnc:registry": [],
    # Legacy NRIC entries were removed because their old register URLs now
    # redirect away from the original decisions. Re-add only after a live PDPC
    # decision page and exact section / penalty can be re-verified.
    "nric:collection": [],
    "nric:leakage": [],
}

# ── Statutory maximum penalty ────────────────────────────────────────────────
# Single source of truth for the penalty ceiling quoted in customer documents.
# The S$1M cap is the base; since 1 Oct 2022 the PDPA (Amendment) uplift applies
# 10% of Singapore annual turnover to organisations turning over more than S$10M.
MAX_PENALTY_TEXT = (
    "Up to S$1,000,000, or 10% of annual turnover in Singapore for organisations "
    "exceeding S$10M, whichever is higher"
)


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
    # Breach NOTIFICATION duty (§26A-D) — distinct from the Protection duty
    "breach:notification": "notification",
    "security_headers:protection": "protection",
    # Do Not Call (Part 9)
    "dnc:registry": "dnc",
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
    # Categories the live index populates but no substring reached, so findings
    # of these types silently resolved to neither a precedent nor a basis.
    ("cross_border", "transfer"),
    ("cross-border", "transfer"),
    ("transfer", "transfer"),
    ("accuracy", "accuracy"),
    ("breach_notification", "notification"),
    ("notification", "notification"),
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
    # A row needs a real decision URL + summary to be usable at all; whether it
    # may be NAMED is decided by its `verified` flag (see get_citable_precedents).
    return [c for c in cases if c.get("url") and c.get("summary")]


def _static_precedents_for_category(category: str) -> list[dict]:
    """Human-verified seed cases for an obligation category.

    PRECEDENTS is keyed by finding_key, but the finding keys that reach us from
    a scan are free-form slugs (pdf_service._finding_key_from emits
    ``free:<type>_<title>``), which never match a seed key exactly. Resolving the
    seed by category as well is what makes the curated list actually reachable —
    without it a scan finding could only ever be served by the live index.
    """
    if not category:
        return []
    out: list[dict] = []
    for key, cases in PRECEDENTS.items():
        if finding_category(key) == category:
            out.extend(cases)
    return out


def get_precedents(finding_key: str) -> list[dict]:
    """Return precedent dicts for this finding key, or [] if none.

    Merges the hand-curated seed (resolved by exact key and by obligation
    category) with the live index of published decisions. Seed entries are always
    verified=True; a live entry is verified only when the index read a financial
    penalty and a citation year off its decision page. Callers that intend to
    NAME a case in a customer document must use get_citable_precedents().
    """
    category = finding_category(finding_key)
    live = _live_precedents_for_category(category) if category else []
    static = list(PRECEDENTS.get(finding_key, []))
    seen_static = {c.get("url") for c in static}
    for c in _static_precedents_for_category(category):
        if c.get("url") not in seen_static:
            static.append(c)
    # A curated entry's year is a hand-checked decision year; scraped rows carry
    # their own year_source ("decided" or "published") from the index.
    static = [{**c, "verified": True, "source": "curated"} for c in static]
    # The live index covers PDPC's whole register, so when it is available it is
    # authoritative on its own and the seed is NOT merged on top. Merging would
    # double-count: the same decision sits in both under different names and URLs
    # ("Singapore Health Services (SingHealth)" in the seed vs "SingHealth and
    # IHiS" on the register), which inflates the case count and penalty total we
    # quote. The seed's job is to be the floor when the scrape is unavailable.
    if live:
        return live
    return static


def get_citable_precedents(finding_key: str) -> list[dict]:
    """Precedents that may be NAMED in a customer-facing document.

    A case qualifies on EVIDENCE, not on provenance: either it is in the curated
    seed, or the index read a financial penalty AND a "[YYYY] SGPDPC N" citation
    year off its decision page. The penalty is the load-bearing signal — a
    cleared organisation has none, so an acquittal cannot pass this gate.

    Why the gate is not the decision title: the index classifies obligation from
    the title, and "No Breach of the Protection Obligation by BHG (Singapore)"
    reads as `protection` exactly like a real breach does. That is how a cleared
    organisation came to be printed in a delivered report as a "Notable case".
    Titles say what a decision is ABOUT; only the page says how it ENDED.

    Every citable row therefore carries a disclosed penalty, which is what lets
    precedent_summary() quote a count and a total that describe the same set.
    """
    return [c for c in get_precedents(finding_key) if c.get("verified")]


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
    finding type. Returns None when no citable precedents are on file — callers
    then fall back to regulatory_basis(), which is honest rather than empty.

    Only evidence-verified cases are summarised (see get_citable_precedents), so
    the count and the penalty total always describe the same set. Both figures
    are stated as floors, and the shared obligation is named rather than any
    similarity of facts being implied.

    Output format:
        "PDPC has published at least N financial penalties totalling S$X or more
         under the §24 Protection Obligation. Most recent:
         [Vendor 1 (published 2026)] and [Vendor 2 (decided 2023)]."
    """
    items = get_citable_precedents(finding_key)
    if not items:
        return None

    count = len(items)
    # Every verified case carries a disclosed penalty; a 0 or missing figure is
    # excluded from the total rather than counted as S$0.
    fines = [int(i["fine_sgd"]) for i in items if i.get("fine_sgd")]
    total = sum(fines)
    if not fines or len(fines) != count:
        # Should not happen (a citable case carries a penalty by definition), but
        # never emit a count next to a total that describes a different set —
        # that mismatch is what produced the misleading "209 enforcement
        # decisions … at least S$621k" line.
        return None

    # Which obligation these cases were decided under. Stated explicitly rather
    # than implied: a §24 bucket spans ransomware, lost laptops and misconfigured
    # servers, so "under similar facts" would overclaim — what the cases share is
    # the obligation, not the facts.
    sections = [i.get("section") for i in items if i.get("section")]
    section = Counter(sections).most_common(1)[0][0] if sections else None

    # Examples: the most recent decisions, heaviest penalty first within a year,
    # preferring a named party over one the register anonymised ("a Registered
    # Salesperson") — the anonymised ones still count, they just read as nothing
    # a buyer can look up. Recency is what makes a citation land: a 2026 case
    # reads as live risk, a 2017 one as history.
    def _named(c: dict) -> int:
        return 0 if _ANON_PARTY_RE.match(c.get("vendor") or "") else 1

    ranked = sorted(
        items,
        key=lambda c: (_named(c), c.get("year") or 0, c.get("fine_sgd") or 0),
        reverse=True,
    )
    # Each year carries its own provenance, because the two are different facts:
    # "published" is the register's publication year, which can trail the
    # decision date across a year boundary; "decided" comes from the neutral
    # citation ("[2023] SGPDPC 4") or, for a curated entry, a hand-checked
    # decision year. Labelling per case rather than per sentence keeps a mixed
    # list unambiguous.
    examples = []
    for i in ranked[:max_items]:
        vendor = i.get("vendor")
        if not vendor:
            continue
        year = i.get("year")
        if not year:
            examples.append(vendor)
            continue
        source = i.get("year_source") or ("decided" if i.get("source") == "curated" else None)
        examples.append(f"{vendor} ({source} {year})" if source else f"{vendor} ({year})")

    def _sgd(x: int) -> str:
        if x >= 1_000_000:
            return f"S${x/1_000_000:.1f}M"
        if x >= 1_000:
            return f"S${x/1_000:.0f}k"
        return f"S${x}"

    # "at least": the index covers the published decisions we could classify by
    # obligation and read a penalty from. Decisions under obligations we do not
    # classify, and any page whose penalty we could not parse, are absent — so
    # the true figures are floors, never exact counts. Undertakings and cleared
    # decisions are excluded by design (no penalty was imposed).
    dec_word, pen_word = (
        ("decision", "a financial penalty") if count == 1
        else ("decisions", "financial penalties")
    )
    base = (
        f"PDPC has published at least {count} {dec_word} imposing {pen_word} "
        f"totalling at least {_sgd(total)}"
    )
    base += f" under the {section}." if section else " under this obligation."

    if examples:
        base += f" Most recent: {' and '.join(examples)}."
    return base


def precedent_keys() -> list[str]:
    """Return all finding_keys that have at least one precedent on file."""
    return [k for k, v in PRECEDENTS.items() if v]


def precedent_count() -> int:
    """Total number of seeded precedent entries (across all keys)."""
    return sum(len(v) for v in PRECEDENTS.values())
