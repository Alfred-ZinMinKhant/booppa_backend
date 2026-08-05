"""Regulator-agnostic registry of the legal instruments our documents cite.

`mas_notice_registry` proved the shape: a citation kept as *data* cannot drift
the way a citation kept as prose does. Every deliverable that names a statute,
notice or regulation is signed and, for several products, hash-anchored — a
wrong citation in one of those is not a typo, it is a false statement about the
customer's own legal obligations, made under our name. Two real instances are on
record: FSM-N05/N06 (the *bank* notices) cited for every MAS licence class, and
tipping-off cited as CDSA s.48A.

This module generalises the registry across issuing bodies — MAS, PDPC, ACRA,
CDSA, IMDA, GovTech, MOF, SAC, FATF — so that:

  * one lookup surface serves every product (TRM, CSP, PDPA, tender);
  * corrections land in one place instead of in each generator's prose; and
  * the tipping-off section is data, so it cannot silently regress the next
    time someone rewrites that paragraph.

**Encoding prose as data preserves the prose's errors.** The first version of
this file was built by lifting citations out of the generators, which is why it
shipped with tipping-off at s.48 (also wrong — the offence is s.57), breach
notification at PDPA Part 6B (it is 6A), the Spam Control Act filed under PDPC
(it is IMDA's), and GeBIZ Trading Partner registration merged with Government
Supplier Registration. Reading all fourteen unverified rows against their
primary sources on 2026-08-05 found five wrong, including the single row that
had been marked verified. A row is only evidence once someone has opened
`source_url` — which is why that field is mandatory whenever `verified_on` is
set, and why the staleness sweep is detection-only.

**Per-instrument `verified_on`, not one date for the table.** The MAS registry
carried a single module-level `REGISTRY_VERIFIED_ON = 2026-08-03` while several
of its `applies_to` sets were wrong — a blanket date certifies nothing, it just
makes the table *look* checked. Here the date belongs to the row, and a row that
has never been checked against the primary source carries `verified_on=None`
rather than borrowing its neighbour's credibility.

`stale_instruments()` is **detection only — it never edits an entry**. Nothing
here may be auto-refreshed: an automated "still current" tick is exactly the
false assurance the per-row date exists to prevent. It reports; a human verifies
against the regulator's own page and moves the date.

Scraping regulators for "last revised" dates is deliberately out of scope. None
of these bodies publishes a machine-readable feed, and a scraper that silently
starts returning nothing looks identical to "everything is current".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

# Issuing bodies. Note that CDSA is a statute rather than a regulator — it is
# administered by the Suspicious Transaction Reporting Office — but our
# documents cite it by Act, and grouping by the name the citation actually uses
# is what keeps the registry greppable from the prose it replaced.
BODY_MAS = "MAS"
BODY_PDPC = "PDPC"
BODY_ACRA = "ACRA"
BODY_CDSA = "CDSA"
BODY_IMDA = "IMDA"
# Split from a former combined "GovTech/MOF" label. The Government Procurement
# Act 1997 is MOF's; GeBIZ is GovTech's. A body string that names two regulators
# is an attribution we cannot stand behind in a signed document, and it is the
# same class of error as filing the Spam Control Act under PDPC.
BODY_GOVTECH = "GovTech"
BODY_MOF = "MOF"
BODY_SAC = "SAC"
BODY_FATF = "FATF"

# How often a human must re-check a row against the primary source. ~a quarter
# plus slack: MAS moved the whole Notice numbering on 10 May 2024 and the
# outsourcing regime on 11 Dec 2024, both with months of lead time, so a
# quarterly sweep catches a change before it reaches a customer document.
DEFAULT_MAX_AGE_DAYS = 100


@dataclass(frozen=True)
class Instrument:
    """One legal instrument, as cited.

    `applies_to` is a set of scope tokens whose vocabulary is the issuing body's
    own: MAS licence classes for MAS, `sg_organisation` / `csp` / `vendor` for
    the rest. An empty set means "we have not established who this binds" and is
    a deliberate, load-bearing value — see MAS FSM-N13, where an empty set is
    what gates the documents that would otherwise over-claim.
    """

    issuing_body: str
    key: str
    ids: Tuple[str, ...]
    title: str
    instrument_type: str  # "notice" | "guidelines" | "act" | "regulations" | "standard"
    binding: bool  # statutes and notices bind; guidelines are guidance
    status: str  # "current" | "superseded"
    applies_to: frozenset
    citation: str
    # Date a human last confirmed this row against the issuing body's own
    # publication. None = never independently verified; such rows are reported
    # as stale from day one rather than presumed good.
    verified_on: Optional[date] = None
    # The page that was actually read to set `verified_on`. Required whenever
    # `verified_on` is set: a date on its own is an unfalsifiable assurance —
    # it says someone checked, not what they checked against, and that is
    # exactly how "CDSA s.48" survived being marked verified. The URL also
    # makes re-verification a click instead of a search.
    source_url: Optional[str] = None
    key_obligations: Tuple[str, ...] = ()
    # Clause references a generated document must address one-for-one. Empty for
    # instruments we do not attest clause-by-clause against.
    clause_anchors: Tuple[str, ...] = ()
    superseded_by: Optional[str] = None
    superseded_on: Optional[date] = None
    binding_from: Optional[date] = None

    @property
    def is_current(self) -> bool:
        return self.status == "current"

    @property
    def ref(self) -> str:
        return f"{self.issuing_body}:{self.key}"


# body -> key -> Instrument. Populated by `register_body`; the MAS table lives
# in `mas_notice_registry` and registers itself on import.
_BODIES: Dict[str, Dict[str, Instrument]] = {}


def register_body(body: str, instruments: Dict[str, Instrument]) -> None:
    """Register (or replace) a body's table.

    Raises on an `issuing_body` mismatch — a row filed under the wrong body is a
    lookup that silently returns another regulator's instrument.
    """
    for key, inst in instruments.items():
        if inst.key != key:
            raise ValueError(f"{body}: key {key!r} filed under {inst.key!r}")
        if inst.issuing_body != body:
            raise ValueError(
                f"{body}: {key!r} declares issuing_body {inst.issuing_body!r}"
            )
        if inst.verified_on is not None and not inst.source_url:
            raise ValueError(
                f"{body}: {key!r} is marked verified_on "
                f"{inst.verified_on.isoformat()} with no source_url"
            )
    _BODIES[body] = dict(instruments)


def _load_all() -> None:
    """Import every data module so a whole-registry sweep sees all of them.

    Lazy and inside the function because `mas_notice_registry` imports this
    module; at import time of *this* module it does not yet exist.
    """
    if BODY_MAS not in _BODIES:
        from app.services import mas_notice_registry  # noqa: F401


def bodies() -> Tuple[str, ...]:
    _load_all()
    return tuple(sorted(_BODIES))


def get(body: str, key: str) -> Instrument:
    """Look up an instrument. Raises KeyError — a typo'd key must not silently
    produce a document with no citation."""
    _load_all()
    try:
        return _BODIES[body][key]
    except KeyError as exc:
        raise KeyError(f"no instrument {body}:{key}") from exc


def cite(body: str, key: str) -> str:
    """The citation string for one instrument. The prose replacement: generators
    call this instead of typing the Act and section into an f-string."""
    return get(body, key).citation


def for_scope(body: str, scope: Optional[str]) -> Tuple[Instrument, ...]:
    """Current instruments of `body` that bind `scope`. Unknown scope returns
    nothing — never a best guess."""
    _load_all()
    if not scope:
        return ()
    return tuple(
        i for i in _BODIES.get(body, {}).values()
        if i.is_current and scope in i.applies_to
    )


def superseded_warning(body: str, key: str) -> Optional[str]:
    """A sentence naming an instrument as cancelled, or None if it is current."""
    inst = get(body, key)
    if inst.is_current:
        return None
    replacement = _BODIES.get(body, {}).get(inst.superseded_by or "")
    on = (
        inst.superseded_on.strftime("%d %B %Y")
        if inst.superseded_on else "an earlier date"
    )
    tail = f" and replaced by {replacement.citation}" if replacement else ""
    return (
        f"{inst.citation.split(' — ')[0]} was cancelled on {on}{tail}. It is no "
        f"longer in force and must not be cited as a current obligation."
    )


def render_citation_block(instruments: Sequence[Instrument]) -> str:
    """Render instruments as a prompt-injectable block.

    Superseded instruments are included *only* with their cancellation stated, so
    the model has an explicit correction rather than a gap its training data
    fills with the old number.
    """
    lines: List[str] = []
    for inst in instruments:
        if not inst.is_current:
            lines.append(
                "- DO NOT CITE AS CURRENT: "
                f"{superseded_warning(inst.issuing_body, inst.key)}"
            )
            continue
        nature = (
            "BINDING (legally enforceable)"
            if inst.binding
            else "GUIDANCE (not legally binding — describe as such)"
        )
        head = f"- {inst.citation} — {nature}"
        if inst.binding_from:
            head += f"; binding from {inst.binding_from.strftime('%d %B %Y')}"
        lines.append(head)
        for ob in inst.key_obligations:
            lines.append(f"    * {ob}")
        if inst.clause_anchors:
            lines.append(
                "    Required clause headings: " + "; ".join(inst.clause_anchors)
            )
    return "\n".join(lines)


def citation_block(refs: Sequence[str]) -> str:
    """`render_citation_block` over `"BODY:key"` references."""
    out = []
    for ref in refs:
        body, _, key = ref.partition(":")
        out.append(get(body, key))
    return render_citation_block(out)


def all_instruments() -> Tuple[Instrument, ...]:
    _load_all()
    return tuple(i for table in _BODIES.values() for i in table.values())


def stale_instruments(
    as_of: Optional[date] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> List[Tuple[Instrument, str]]:
    """Instruments due for human re-verification, with the reason.

    **Detection only. This function never edits an instrument**, and nothing
    downstream of it may either: the whole point of the date is that a human
    looked at the regulator's page, so a process that moves the date without a
    human having looked converts the registry from evidence into decoration.

    Superseded rows are skipped — they are historical by construction and are
    only ever cited *as* cancelled.
    """
    today = as_of or date.today()
    out: List[Tuple[Instrument, str]] = []
    for inst in all_instruments():
        if not inst.is_current:
            continue
        if inst.verified_on is None:
            out.append((inst, "never verified against the primary source"))
            continue
        age = (today - inst.verified_on).days
        if age > max_age_days:
            out.append((
                inst,
                f"last verified {inst.verified_on.isoformat()} ({age} days ago, "
                f"limit {max_age_days})",
            ))
    return sorted(out, key=lambda p: p[0].ref)


# ---------------------------------------------------------------------------
# PDPC — Personal Data Protection Act 2012 and related
# ---------------------------------------------------------------------------

_SG_ORG = frozenset({"sg_organisation"})

PDPC_INSTRUMENTS: Dict[str, Instrument] = {
    "pdpa": Instrument(
        issuing_body=BODY_PDPC,
        key="pdpa",
        ids=("PDPA", "Act 26 of 2012"),
        title="Personal Data Protection Act 2012",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_SG_ORG,
        citation="Personal Data Protection Act 2012 (PDPA)",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/PDPA2012",
        key_obligations=(
            "Consent, purpose limitation and notification before collecting, "
            "using or disclosing personal data.",
            "Reasonable security arrangements to protect personal data in the "
            "organisation's possession or under its control.",
            "Accountability: designate a Data Protection Officer and publish "
            "their business contact information.",
            "Retention limitation: cease retention once the purpose is served "
            "and retention is no longer necessary for legal or business needs.",
        ),
    ),
    "pdpa_breach_notification": Instrument(
        issuing_body=BODY_PDPC,
        key="pdpa_breach_notification",
        ids=("PDPA Part 6A", "s.26A-26E"),
        title="Data Breach Notification",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_SG_ORG,
        # Part 6A, not 6B. Part 6B is DATA PORTABILITY; this row shipped citing
        # it because the two Parts were inserted by the same 2020 amendment and
        # sit adjacent in the Act. The section range (26A-26E) was right the
        # whole time, which is what made the wrong Part hard to see.
        citation="PDPA Part 6A (sections 26A–26E) — Data Breach Notification",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/PDPA2012",
        key_obligations=(
            "Assess whether a data breach is notifiable no later than 30 "
            "calendar days after becoming aware of it.",
            "Notify PDPC no later than 3 calendar days after determining the "
            "breach is notifiable.",
            "Notify affected individuals where the breach is likely to result "
            "in significant harm to them.",
        ),
    ),
    "pdpa_s48j": Instrument(
        issuing_body=BODY_PDPC,
        key="pdpa_s48j",
        ids=("PDPA s.48J",),
        title="Financial penalties — duration of non-compliance",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_SG_ORG,
        citation="section 48J(6) of the PDPA",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/PDPA2012",
        # s.48J(6) is the one our Monitor delta report leans on: it is why an
        # unresolved finding gets worse purely by remaining unresolved, which is
        # the entire premise of a month-over-month deliverable.
        key_obligations=(
            "In deciding the amount of a financial penalty, PDPC must have "
            "regard to (among other matters) the duration of the "
            "non-compliance — section 48J(6).",
        ),
    ),
    "pdpa_dnc": Instrument(
        issuing_body=BODY_PDPC,
        key="pdpa_dnc",
        ids=("PDPA Part 9",),
        title="Do Not Call Registry",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_SG_ORG,
        citation="PDPA Part 9 — Do Not Call Registry",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/PDPA2012",
        key_obligations=(
            "Check the relevant Do Not Call Register before sending a specified "
            "message to a Singapore telephone number.",
            "Include clear and accurate identification of the sender and "
            "contact information in the message.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# IMDA — electronic marketing
# ---------------------------------------------------------------------------

# The Spam Control Act 2007 sat in the PDPC table until 2026-08-05. It is
# administered by IMDA, not PDPC — the two get conflated because unsolicited
# marketing engages both the Act and the PDPA's Do Not Call provisions, and the
# DNC Registry *is* PDPC's. `register_body` cannot catch this: it checks that a
# row's declared issuing_body matches the table it sits in, not that the
# attribution is true. Only reading the issuing body's own page catches it.
IMDA_INSTRUMENTS: Dict[str, Instrument] = {
    "spam_control_act": Instrument(
        issuing_body=BODY_IMDA,
        key="spam_control_act",
        ids=("Spam Control Act 2007",),
        title="Spam Control Act 2007",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_SG_ORG,
        citation="Spam Control Act 2007",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/SCA2007",
        key_obligations=(
            "Unsubscribe facility in bulk electronic marketing messages, "
            "honoured within 10 business days.",
            "Accurate sender information and an <ADV> label where required.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# ACRA — corporate service providers and nominee directors
# ---------------------------------------------------------------------------

_CSP = frozenset({"csp"})

ACRA_INSTRUMENTS: Dict[str, Instrument] = {
    "csp_act_2024": Instrument(
        issuing_body=BODY_ACRA,
        key="csp_act_2024",
        ids=("CSP Act 2024", "Act 22 of 2024"),
        title="Corporate Service Providers Act 2024",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_CSP,
        citation="Corporate Service Providers Act 2024 (CSP Act 2024)",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/CSPA2024",
        binding_from=date(2025, 6, 9),
        key_obligations=(
            "A corporate service provider carrying on business in Singapore "
            "must itself be registered with ACRA.",
            "Perform customer due diligence and keep the records that evidence "
            "it.",
            "Appoint at least one Registered Qualified Individual (RQI) "
            "responsible for the CSP's compliance.",
            "Assess the fitness and propriety of any individual acting as a "
            "nominee director arranged by the CSP.",
        ),
    ),
    "csp_regulations_2025": Instrument(
        issuing_body=BODY_ACRA,
        key="csp_regulations_2025",
        ids=("CSP Regulations 2025", "S 292/2025"),
        title="Corporate Service Providers Regulations 2025",
        instrument_type="regulations",
        binding=True,
        status="current",
        applies_to=_CSP,
        citation="Corporate Service Providers Regulations 2025 (S 292/2025)",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/SL/CSPA2024-S292-2025",
        binding_from=date(2025, 6, 9),
        key_obligations=(
            "Record-keeping form and retention period for customer due "
            "diligence records.",
            "Conditions and ongoing obligations attaching to CSP registration.",
        ),
    ),
    "nominee_director_disclosure": Instrument(
        issuing_body=BODY_ACRA,
        key="nominee_director_disclosure",
        ids=(
            "Companies Act 1967 s.145A",
            "Companies Act 1967 s.386AKA",
            "Companies Act 1967 s.386ALA",
            "CSP Regulations 2025 reg. 20",
        ),
        title="Nominee director and nominee shareholder disclosure",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_CSP,
        # Was "ACRA nominee director disclosure requirement" with
        # binding_from=2025-06-16 — a paraphrase of an ACRA webpage rather than
        # a citation, and a date that was a week out. The obligations are real
        # sections of the Companies Act 1967 (inserted by the Companies and
        # Limited Liability Partnerships (Miscellaneous Amendments) Act 2024)
        # plus reg. 20 of the CSP Regulations 2025, all commencing 9 June 2025
        # with the CSP Act.
        citation=(
            "sections 145A, 386AKA and 386ALA of the Companies Act 1967, read "
            "with regulation 20 of the Corporate Service Providers Regulations "
            "2025"
        ),
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/CoA1967",
        binding_from=date(2025, 6, 9),
        key_obligations=(
            "An individual must not act as a nominee director by way of "
            "business unless the appointment was arranged by a registered "
            "corporate service provider that assessed them as fit and proper "
            "— section 145A.",
            "Companies must keep a register of nominee directors and a "
            "register of nominee shareholders identifying each nominator, and "
            "lodge that information with ACRA — sections 386AKA and 386ALA.",
            "Entities incorporated before 9 June 2025 had until 31 December "
            "2025 to lodge their nominee registers with ACRA.",
        ),
    ),
    "rqi_registration": Instrument(
        issuing_body=BODY_ACRA,
        key="rqi_registration",
        ids=("CSP Act 2024 ss.10-11",),
        title="Registered Qualified Individual",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_CSP,
        citation=(
            "Registered Qualified Individual (RQI) registration under "
            "sections 10 and 11 of the CSP Act 2024"
        ),
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/CSPA2024",
        binding_from=date(2025, 6, 9),
        key_obligations=(
            "The RQI must be registered with ACRA in that capacity — "
            "registration is personal and cannot be satisfied by a document.",
            "The fit-and-proper assessment of a nominee director must be "
            "performed by the registered RQI, not generated.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# CDSA — the tipping-off section, encoded so it cannot regress again
# ---------------------------------------------------------------------------

CDSA_INSTRUMENTS: Dict[str, Instrument] = {
    "tipping_off": Instrument(
        issuing_body=BODY_CDSA,
        key="tipping_off",
        ids=("CDSA s.57",),
        title="Tipping-off",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_CSP,
        # Third value for this one number, so the history matters:
        #   v1 CSP packs said s.48A — that is Part VIA, cross-border movement
        #     of cash reporting.
        #   v2 "corrected" it to s.48 — that is "Communication of information
        #     to foreign authority". Also wrong, and it was this row, marked
        #     verified_on, that carried it.
        #   The offence is s.57 "Tipping-off", in Part 6 OFFENCES of the 2020
        #     Revised Edition, confirmed against the SSO table of contents on
        #     2026-08-05.
        # The reason it keeps drifting is that nobody was reading the Act — the
        # number was being copied between our own prose, a law-firm note and a
        # model completion, each of which looks like a source. Anything naming
        # the section must read it from this row; see the _BAD_CITATION guard
        # in csp_doc_generator.py, which now rejects s.48A *and* bare s.48.
        citation=(
            "section 57 of the Corruption, Drug Trafficking and Other Serious "
            "Crimes (Confiscation of Benefits) Act 1992 (CDSA)"
        ),
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/CDTOSCCBA1992",
        key_obligations=(
            "Disclosing to any person information likely to prejudice an "
            "investigation arising from a suspicious transaction report is an "
            "offence.",
        ),
    ),
    "str_reporting": Instrument(
        issuing_body=BODY_CDSA,
        key="str_reporting",
        ids=("CDSA s.45",),
        title="Suspicious transaction reporting",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_CSP,
        citation="section 45 of the CDSA — suspicious transaction reporting",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/CDTOSCCBA1992",
        key_obligations=(
            "File a suspicious transaction report with the Suspicious "
            "Transaction Reporting Office where there are reasonable grounds "
            "to suspect that property represents the proceeds of, or is used "
            "in connection with, criminal conduct.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# MOF — public sector procurement legislation and supplier registration
# ---------------------------------------------------------------------------

_VENDOR = frozenset({"vendor"})

MOF_INSTRUMENTS: Dict[str, Instrument] = {
    "government_procurement_act": Instrument(
        issuing_body=BODY_MOF,
        key="government_procurement_act",
        ids=("Government Procurement Act 1997",),
        title="Government Procurement Act 1997",
        instrument_type="act",
        binding=True,
        status="current",
        applies_to=_VENDOR,
        citation="Government Procurement Act 1997",
        verified_on=date(2026, 8, 5),
        source_url="https://sso.agc.gov.sg/Act/GPA1997",
        key_obligations=(
            "Open, fair and transparent treatment of suppliers in government "
            "procurement covered by the Act.",
        ),
    ),
    "supplier_registration": Instrument(
        issuing_body=BODY_MOF,
        key="supplier_registration",
        ids=("GSR",),
        title="Government Supplier Registration",
        instrument_type="standard",
        binding=False,
        status="current",
        applies_to=_VENDOR,
        # Split out of a single "GeBIZ supplier registration" row that merged
        # two different schemes and named a "GeBIZ supply category" that
        # neither of them uses. GSR (this row) is MOF's — BCA's for
        # construction — and is a *tender criterion*, not a precondition for
        # access. GTP (GovTech, below) is the access requirement. Telling a
        # vendor they must hold GSR to quote is a false barrier; telling them
        # GTP suffices for a GSR-gated tender loses them the bid.
        citation="Government Supplier Registration (GSR)",
        verified_on=date(2026, 8, 5),
        source_url="https://www.gebiz.gov.sg/",
        key_obligations=(
            "Required only where a particular tender or quotation names a GSR "
            "head and grade in its criteria — it is not needed to access "
            "GeBIZ or to bid generally.",
            "Administered by MOF for general goods and services, and by BCA "
            "for construction and construction-related work.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# GovTech — GeBIZ, the procurement platform itself
# ---------------------------------------------------------------------------

GOVTECH_INSTRUMENTS: Dict[str, Instrument] = {
    "gebiz_registration": Instrument(
        issuing_body=BODY_GOVTECH,
        key="gebiz_registration",
        ids=("GeBIZ Trading Partner", "GTP"),
        title="GeBIZ Trading Partner registration",
        instrument_type="standard",
        binding=False,
        status="current",
        applies_to=_VENDOR,
        citation="GeBIZ Trading Partner (GTP) registration",
        verified_on=date(2026, 8, 5),
        source_url="https://www.gebiz.gov.sg/",
        key_obligations=(
            "GTP registration is what lets a supplier download tender "
            "documents and submit a bid or quotation on GeBIZ.",
            "The first user account is free; additional accounts are chargeable.",
        ),
    ),
}

# ---------------------------------------------------------------------------
# SAC / FATF — accreditation and international standards
# ---------------------------------------------------------------------------

SAC_INSTRUMENTS: Dict[str, Instrument] = {
    "sac_accreditation": Instrument(
        issuing_body=BODY_SAC,
        key="sac_accreditation",
        ids=("SAC",),
        title="Singapore Accreditation Council accreditation",
        instrument_type="standard",
        binding=False,
        status="current",
        applies_to=frozenset({"vendor", "csp"}),
        citation="Singapore Accreditation Council (SAC) accreditation",
        verified_on=date(2026, 8, 5),
        source_url="https://www.sac-accreditation.gov.sg/",
        key_obligations=(
            "Certification carries weight only where the certification body is "
            "itself SAC-accredited for that scheme and scope.",
        ),
    ),
}

FATF_INSTRUMENTS: Dict[str, Instrument] = {
    "fatf_recommendations": Instrument(
        issuing_body=BODY_FATF,
        key="fatf_recommendations",
        ids=("FATF Recommendations 2012",),
        title="FATF Recommendations (2012, as amended June 2026)",
        instrument_type="standard",
        # Binds countries, not firms directly. Describing it as binding on a
        # customer would be the same over-claim as citing a Notice that does
        # not apply to their licence class.
        binding=False,
        status="current",
        applies_to=frozenset({"csp", "sg_organisation"}),
        citation=(
            "FATF Recommendations 2012 (as amended June 2026) — international "
            "standards addressed to countries, given effect in Singapore "
            "through the CDSA and sector regulations"
        ),
        verified_on=date(2026, 8, 5),
        source_url=(
            "https://www.fatf-gafi.org/en/publications/Fatfrecommendations/"
            "Fatf-recommendations.html"
        ),
        key_obligations=(
            "Risk-based customer due diligence, including identification of "
            "beneficial ownership.",
            "Ongoing monitoring and reporting of suspicious transactions.",
        ),
    ),
}


register_body(BODY_PDPC, PDPC_INSTRUMENTS)
register_body(BODY_ACRA, ACRA_INSTRUMENTS)
register_body(BODY_CDSA, CDSA_INSTRUMENTS)
register_body(BODY_IMDA, IMDA_INSTRUMENTS)
register_body(BODY_MOF, MOF_INSTRUMENTS)
register_body(BODY_GOVTECH, GOVTECH_INSTRUMENTS)
register_body(BODY_SAC, SAC_INSTRUMENTS)
register_body(BODY_FATF, FATF_INSTRUMENTS)
