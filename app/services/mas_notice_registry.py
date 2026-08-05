"""Structured registry of the MAS instruments the TRM products cite.

Why this exists as data rather than prose: the TRM Guidelines are *guidance*.
The binding instruments are the Notices, and the Notices move. Cyber Hygiene
Notice 655 was cancelled on 10 May 2024 and replaced by FSM-N06; the outsourcing
regime split on 11 Dec 2024 into binding Notices 658 (banks) / 1121 (merchant
banks) versus the Guidelines on Outsourcing (FIs other than Banks). Prose
scattered across trm_workflow_service.py, trm_demo_harness.py and
booppa_ai_service.py cannot be kept in step with that, and a stale citation in a
signed attestation is worse than no citation because it looks authoritative.

``citation_block()`` is the mechanism that keeps the model off the cancelled
notice: 655 is never handed to a prompt as current. It is handed to the prompt
*labelled superseded*, precisely so the model cannot silently reproduce it from
its own training data as if it still bound anyone.

This is now the MAS *data* module for the regulator-agnostic
`regulatory_registry`: the `Instrument` shape, the lookup helpers and the
staleness sweep live there and cover PDPC/ACRA/CDSA/GovTech/SAC/FATF too. The
MAS-specific resolution rules — which Notice binds which licence class, and the
refusal to guess one — stay here, because they are the part that is genuinely
MAS-shaped.

Shelf life: each entry carries its own `verified_on`, re-checked against the MAS
website. It is deliberately per-entry: the single module-level date this file
used to carry read "verified 2026-08-03" while several `applies_to` sets were
wrong, which is what a table-wide date buys you.

Provenance (2026-08-05): every row here previously carried verified_on 3-5 Aug
2026 with no `source_url`, because the field did not exist yet. A date with no
URL cannot be re-checked and cannot be falsified, so it was not evidence of
verification. All sixteen rows were then re-read against their own page on
mas.gov.sg the same day, and **five were wrong** — the same hit rate as the
non-MAS tables, which is the argument for the `source_url` requirement rather
than an argument about MAS:

  * Notice 655 was recorded as applying to every licence class. It applied to
    banks only (Full/Wholesale Bank), under Banking Act (Cap. 19) s.55(1).
  * Notices 658 and 1121 are named "Management of Outsourced Relevant Services
    for (Merchant) Banks". "Outsourcing (Banks)" was our paraphrase, and a
    paraphrase printed as a citation is not a citation.
  * The TRM Guidelines are published as "Guidelines on Risk Management
    Practices - Technology Risk"; "Technology Risk Management Guidelines" is
    the colloquial name, not the title on the document.
  * FSM-N03 and FSM-N04 do **not** share a scope, though both were labelled
    "(Insurers)": FSM-N03 excludes captive and marine mutual insurers, while
    FSM-N04 reaches captive insurers *and* general insurance agents.

The FSM-N03/N04 asymmetry was the FSM-N05-for-everyone bug in miniature: one
licence key ("insurer") mapped to two Notices whose scopes differ, so a captive
insurer was told FSM-N03 binds it when the Notice carves it out.

Both of the follow-ups that pass left open are now closed (2026-08-05, same
sources re-read for the "Applies to" lists rather than trusted from the earlier
reading):

  * The `insurer` licence key was split. It now means the six classes FSM-N03
    actually lists — Direct Insurer and Reinsurer, each Life/General/Composite —
    with `captive_insurer`, `insurance_agent` and `marine_mutual_insurer` as
    separate keys. Captives and agents map to FSM-N04 only; marine mutuals, who
    appear in neither Notice, map to nothing and gate.
  * FSM-N31 (Cyber Hygiene, digital token service providers, effective
    30 June 2025) is now a row rather than a docstring note, together with a
    `dtsp` licence key. It gates for technology risk management: no FSM-series
    TRM Notice for DTSPs was found, and FSM-N13 speaks to the Payment Services
    Act digital-payment-token licence, which is a different regime.

Both changes alter gating behaviour: entities that previously resolved to a
Notice may now correctly resolve to none, and the caller must handle the
ValueError by asking rather than defaulting.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Optional, Sequence, Tuple

from app.services.mas_licence import (
    MAS_LICENCE_TYPES,
    REGIME_BANK_NOTICES,
    normalise_licence,
    outsourcing_regime,
)
from app.services.regulatory_registry import (
    BODY_MAS,
    Instrument,
    register_body,
    render_citation_block,
)
from app.services.regulatory_registry import (
    superseded_warning as _generic_superseded_warning,
)

# Kept as an alias: this table's rows are ordinary registry Instruments, and
# callers/tests that annotate with the old name keep working.
MasInstrument = Instrument

_ALL_LICENCES = frozenset(MAS_LICENCE_TYPES)
_NON_BANK = frozenset(k for k in MAS_LICENCE_TYPES if k not in {"bank", "merchant_bank"})


def _mas(**kwargs) -> Instrument:
    return Instrument(issuing_body=BODY_MAS, **kwargs)


MAS_INSTRUMENTS: Dict[str, MasInstrument] = {
    "fsm_n06": _mas(
        key="fsm_n06",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n06",
        ids=("FSM-N06",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"bank"}),
        citation="MAS Notice FSM-N06 — Cyber Hygiene",
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        # One attestation table row is required per anchor. These are the six
        # substantive requirement headings of the notice.
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "notice_655": _mas(
        key="notice_655",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-655",
        ids=("655",),
        title="Cyber Hygiene (cancelled)",
        instrument_type="notice",
        binding=False,
        status="superseded",
        # Banks only, not every licence class. MAS's own page lists Full Bank
        # (Branch/Locally Incorporated) and Wholesale Bank (Branch/Locally
        # Incorporated), issued under Banking Act (Cap. 19) s.55(1). The row
        # shipped as _ALL_LICENCES, which would have told a payment institution
        # it was once bound by a Notice that never reached it.
        applies_to=frozenset({"bank"}),
        citation=(
            "MAS Notice 655 — Cyber Hygiene (banks; cancelled 10 May 2024, "
            "replaced by FSM-N06)"
        ),
        superseded_by="fsm_n06",
        superseded_on=date(2024, 5, 10),
    ),
    "fsm_n05": _mas(
        key="fsm_n05",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n05",
        ids=("FSM-N05", "644"),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"bank"}),
        citation="MAS Notice FSM-N05 (formerly 644) — Technology Risk Management",
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "fsm_n03": _mas(
        key="fsm_n03",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n03",
        ids=("FSM-N03",),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        # MAS's "Applies to" list, read 2026-08-05: Direct Insurer (Life,
        # General, Composite) and Reinsurer (Life, General, Composite) — the
        # six classes the `insurer` key now means, and nothing else. Captives,
        # marine mutuals and general insurance agents each have their own key
        # and are deliberately absent here; see mas_licence.MAS_LICENCE_TYPES.
        applies_to=frozenset({"insurer"}),
        citation=(
            "MAS Notice FSM-N03 — Technology Risk Management (direct insurers "
            "and reinsurers; not captive insurers, marine mutual insurers or "
            "general insurance agents)"
        ),
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "fsm_n04": _mas(
        key="fsm_n04",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n04",
        ids=("FSM-N04",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        # Strictly wider than FSM-N03: MAS lists the same six direct-insurer
        # and reinsurer classes PLUS Captive Insurer and General Insurance
        # Agents. Marine mutual insurers are in neither Notice's list.
        applies_to=frozenset({"insurer", "captive_insurer", "insurance_agent"}),
        citation=(
            "MAS Notice FSM-N04 — Cyber Hygiene (direct insurers, reinsurers, "
            "captive insurers and general insurance agents)"
        ),
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "fsm_n11": _mas(
        key="fsm_n11",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n11",
        ids=("FSM-N11",),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"merchant_bank"}),
        citation="MAS Notice FSM-N11 — Technology Risk Management (Merchant Banks)",
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "fsm_n12": _mas(
        key="fsm_n12",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n12",
        ids=("FSM-N12",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"merchant_bank"}),
        citation="MAS Notice FSM-N12 — Cyber Hygiene (Merchant Banks)",
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "fsm_n13": _mas(
        key="fsm_n13",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n13",
        ids=("FSM-N13",),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        # Narrower than "payment licensees". FSM-N13 binds operators and
        # settlement institutions of designated payment systems, and holders of
        # a payment services licence for digital payment token service. A plain
        # MPI/SPI that is neither is not in scope, which is why _TRM_NOTICE_KEYS
        # has no mpi/spi entry — `applies_to` here records who the Notice
        # actually binds, not who holds a payment licence.
        applies_to=frozenset(),
        citation=(
            "MAS Notice FSM-N13 — Technology Risk Management (operators and "
            "settlement institutions of designated payment systems, and holders "
            "of a payment services licence for digital payment token service)"
        ),
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "fsm_n14": _mas(
        key="fsm_n14",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n14",
        ids=("FSM-N14",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"mpi", "spi"}),
        citation=(
            "MAS Notice FSM-N14 — Cyber Hygiene "
            "(Payment Services Licensees / DPT Service Providers)"
        ),
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "fsm_n21": _mas(
        key="fsm_n21",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n21",
        ids=("FSM-N21",),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"capital_markets"}),
        citation="MAS Notice FSM-N21 — Technology Risk Management (Capital Markets FIs)",
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "fsm_n22": _mas(
        key="fsm_n22",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-fsm-n22",
        ids=("FSM-N22",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"capital_markets"}),
        citation="MAS Notice FSM-N22 — Cyber Hygiene (Capital Markets FIs)",
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "fsm_n31": _mas(
        key="fsm_n31",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/fsm-n31-cyber-hygiene",
        ids=("FSM-N31",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        # MAS's "Applies to" list is one entry: Digital Token Service Provider.
        # This is the FSM Act 2022 DTSP regime, not the Payment Services Act
        # digital-payment-token licence FSM-N13 speaks to; the two must not be
        # collapsed. Published 30 May 2025, in effect from 30 June 2025.
        applies_to=frozenset({"dtsp"}),
        citation=(
            "MAS Notice FSM-N31 — Cyber Hygiene (digital token service "
            "providers; effective 30 June 2025)"
        ),
        key_obligations=(
            "Administrative accounts secured against unauthorised access.",
            "Security patches applied to address vulnerabilities in a timely manner.",
            "Written set of security standards for every system, with regular review.",
            "Network perimeter defence to restrict unauthorised network traffic.",
            "Malware protection on systems, where such measures are available.",
            "Multi-factor authentication for administrative access to critical systems, "
            "and for online access by customers to their accounts.",
        ),
        clause_anchors=(
            "Administrative Accounts",
            "Security Patches",
            "Security Standards",
            "Network Perimeter Defence",
            "Malware Protection",
            "Multi-Factor Authentication",
        ),
    ),
    "notice_658": _mas(
        key="notice_658",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-658",
        ids=("658",),
        # MAS's title, not our paraphrase of it. The row read "Outsourcing
        # (Banks)", which is what the regime is colloquially called; the Notice
        # is issued under Banking Act 1970 s.47A and is named for *outsourced
        # relevant services*, a defined term narrower than "outsourcing".
        title="Management of Outsourced Relevant Services for Banks",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"bank"}),
        citation=(
            "MAS Notice 658 — Management of Outsourced Relevant Services for "
            "Banks (Banking Act 1970 s.47A; effective 11 December 2024)"
        ),
        binding_from=date(2024, 12, 11),
        key_obligations=(
            "Maintain a register of all outsourcing arrangements in the format "
            "specified by MAS.",
            "Submit the register to MAS on request and keep it current.",
            "Board or senior management approval of material outsourcing arrangements.",
            "Assess and record sub-outsourcing by the service provider.",
        ),
    ),
    "notice_1121": _mas(
        key="notice_1121",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/notices/notice-1121",
        ids=("1121",),
        # See notice_658 — same correction, same reason.
        title="Management of Outsourced Relevant Services for Merchant Banks",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"merchant_bank"}),
        citation=(
            "MAS Notice 1121 — Management of Outsourced Relevant Services for "
            "Merchant Banks (Banking Act 1970 s.47A read with s.55ZJ; "
            "effective 11 December 2024)"
        ),
        binding_from=date(2024, 12, 11),
        key_obligations=(
            "Maintain a register of all outsourcing arrangements in the format "
            "specified by MAS.",
            "Submit the register to MAS on request and keep it current.",
            "Board or senior management approval of material outsourcing arrangements.",
            "Assess and record sub-outsourcing by the service provider.",
        ),
    ),
    "outsourcing_guidelines_nonbank": _mas(
        key="outsourcing_guidelines_nonbank",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/guidelines/guidelines-on-outsourcing-financial-institutions-other-than-banks",
        ids=(),
        title="Guidelines on Outsourcing (Financial Institutions other than Banks)",
        instrument_type="guidelines",
        binding=False,
        status="current",
        applies_to=_NON_BANK,
        citation=(
            "MAS Guidelines on Outsourcing (Financial Institutions other than Banks)"
        ),
        key_obligations=(
            "Maintain a register of outsourcing arrangements covering at minimum the "
            "service provider, the service, materiality and the review date.",
            "Board and senior management remain accountable for outsourced functions.",
            "Risk assessment and due diligence before entering an arrangement.",
            "Written agreement addressing audit and inspection rights, including MAS "
            "access.",
        ),
    ),
    "trm_guidelines": _mas(
        key="trm_guidelines",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/guidelines/technology-risk-management-guidelines",
        ids=(),
        # Published title, not the colloquial one. Everyone (including MAS's own
        # press material) says "TRM Guidelines"; the document is issued as
        # "Guidelines on Risk Management Practices - Technology Risk",
        # 18 January 2021, and that is what belongs in a signed citation.
        title="Guidelines on Risk Management Practices — Technology Risk",
        instrument_type="guidelines",
        binding=False,
        status="current",
        applies_to=_ALL_LICENCES,
        citation=(
            "MAS Guidelines on Risk Management Practices — Technology Risk "
            "(issued 18 January 2021; commonly cited as the TRM Guidelines)"
        ),
        key_obligations=(
            "Board and senior management oversight of technology risk.",
            "Technology risk management framework covering identification, "
            "assessment, treatment and monitoring.",
            "Independent audit and regular vulnerability assessment and penetration "
            "testing of critical systems.",
        ),
    ),
    "bcm_guidelines": _mas(
        key="bcm_guidelines",
        verified_on=date(2026, 8, 5),
        source_url="https://www.mas.gov.sg/regulation/guidelines/guidelines-on-business-continuity-management",
        ids=(),
        title="Guidelines on Business Continuity Management",
        instrument_type="guidelines",
        binding=False,
        status="current",
        applies_to=_ALL_LICENCES,
        citation="MAS Guidelines on Business Continuity Management",
        key_obligations=(
            "Identify critical business services and set recovery time objectives.",
            "Service recovery time objectives supported by tested recovery plans.",
            "Board and senior management accountable for business continuity "
            "preparedness; annual attestation.",
            "Testing of business continuity plans, with results reported to the board.",
        ),
    ),
}

register_body(BODY_MAS, MAS_INSTRUMENTS)


def get(key: str) -> MasInstrument:
    """Look up an instrument. Raises KeyError — a typo'd key must not silently
    produce a document with no citation."""
    return MAS_INSTRUMENTS[key]


def for_licence(licence: Optional[str]) -> Tuple[MasInstrument, ...]:
    """Current instruments applicable to a licence class. Unknown licence returns
    only the instruments that apply to every FI (never an outsourcing one)."""
    key = normalise_licence(licence)
    if key is None:
        return tuple(
            i for i in MAS_INSTRUMENTS.values()
            if i.is_current and i.applies_to == _ALL_LICENCES
        )
    return tuple(
        i for i in MAS_INSTRUMENTS.values() if i.is_current and key in i.applies_to
    )


def outsourcing_instruments(licence: Optional[str]) -> Tuple[MasInstrument, ...]:
    """The outsourcing instrument(s) binding this licence class.

    Delegates the fork to ``mas_licence.outsourcing_regime``, which raises on an
    unknown licence. Do not add a fallback here — the register must be gated,
    not guessed.
    """
    regime = outsourcing_regime(licence)
    key = normalise_licence(licence)
    if regime == REGIME_BANK_NOTICES:
        return (get("notice_658") if key == "bank" else get("notice_1121"),)
    return (get("outsourcing_guidelines_nonbank"),)


# Licence key → instrument key, one map per Notice family. Each MAS-regulated
# entity type has its own Notices; there is no single "TRM Notice" or "Cyber
# Hygiene Notice" that applies across licence classes.
#
# The two maps are deliberately NOT a single (trm, cyber_hygiene) pair, because
# the two families do not have the same coverage — see _TRM_NOTICE_KEYS on
# mpi/spi.
#
# Neither map has an entry for "other_fi": it is a catch-all for licence classes
# we have not mapped individually (credit bureaus, insurance brokers, finance
# companies, financial advisers, ...), each with its own Notices that have not
# been confirmed here. Guessing one is the same mistake outsourcing_regime()
# already refuses to make.
_CYBER_HYGIENE_KEYS: Dict[str, str] = {
    "bank": "fsm_n06",
    "merchant_bank": "fsm_n12",
    "mpi": "fsm_n14",
    "spi": "fsm_n14",
    "insurer": "fsm_n04",
    "captive_insurer": "fsm_n04",
    "insurance_agent": "fsm_n04",
    "capital_markets": "fsm_n22",
    "dtsp": "fsm_n31",
}
# No "marine_mutual_insurer" entry in either map: MAS lists marine mutuals in
# the "Applies to" of neither FSM-N03 nor FSM-N04, so there is no cyber-hygiene
# Notice to name. Gating is the answer, and the licence key exists so that the
# gate can fire instead of the entity being read as a plain insurer.

# No mpi/spi entry, on purpose. FSM-N14 (cyber hygiene) does cover payment
# licensees — MAS's own cancellation page for Notice PSN06 redirects them to it.
# FSM-N13 (technology risk management) does not follow the same scope: it binds
# operators and settlement institutions of *designated payment systems* and
# holders of a payment services licence for *digital payment token service*, and
# its predecessor PSN05 was narrower still. A plain major/standard payment
# institution that is neither may have no binding TRM Notice at all, only the
# TRM Guidelines. Asserting FSM-N13 binds it would be the same category of error
# as the FSM-N05-for-everyone bug this map exists to fix, so the TRM-Notice
# documents gate for mpi/spi until the scope is sourced properly.
_TRM_NOTICE_KEYS: Dict[str, str] = {
    "bank": "fsm_n05",
    "merchant_bank": "fsm_n11",
    "insurer": "fsm_n03",
    "capital_markets": "fsm_n21",
}
# Also absent here, each for a sourced reason rather than an oversight:
# captive insurers, marine mutual insurers and general insurance agents are not
# in FSM-N03's "Applies to" list (captives and agents are in FSM-N04's, which is
# why they appear above); and no FSM-series TRM Notice was found for DTSPs when
# FSM-N31 was verified, so `dtsp` gates here while being mapped for cyber
# hygiene. Same shape as mpi/spi: mapped for one family, gated for the other.


def _resolve_notice_key(
    licence: Optional[str], table: Dict[str, str], family: str, documents: str,
) -> str:
    key = normalise_licence(licence)
    if key is None or key not in table:
        raise ValueError(
            f"No MAS {family} Notice is mapped for licence class {key!r}. "
            f"Gate the {documents} and ask the customer to confirm their exact "
            f"licence class rather than defaulting to the bank Notice."
        )
    return table[key]


def technology_risk_instrument(licence: Optional[str]) -> MasInstrument:
    """The binding Technology Risk Management Notice for this licence class.

    Raises on an unknown or unmapped licence, same discipline as
    ``outsourcing_instruments`` — there is no safe default across licence
    classes, only FSM-N05 for banks, FSM-N03 for insurers, etc. Note that
    mpi/spi are unmapped here while being mapped in
    ``cyber_hygiene_instrument``; see _TRM_NOTICE_KEYS.
    """
    return get(_resolve_notice_key(
        licence, _TRM_NOTICE_KEYS, "Technology Risk Management",
        "Governance Policy, Incident Response Plan and BCM/DR Plan",
    ))


def cyber_hygiene_instrument(licence: Optional[str]) -> MasInstrument:
    """The binding Cyber Hygiene Notice for this licence class. Raises on an
    unknown or unmapped licence — see ``technology_risk_instrument``."""
    return get(_resolve_notice_key(
        licence, _CYBER_HYGIENE_KEYS, "Cyber Hygiene",
        "Cyber Hygiene Attestation and Access Control Policy",
    ))


def superseded_warning(key: str) -> Optional[str]:
    """A sentence naming an instrument as cancelled, or None if it is current."""
    return _generic_superseded_warning(BODY_MAS, key)


def citation_block(keys: Sequence[str]) -> str:
    """Render MAS instruments as a prompt-injectable block.

    Superseded instruments are included *only* with their cancellation stated, so
    the model has an explicit correction rather than a gap its training data
    fills with the old notice number — Notice 655 is the case this exists for.
    """
    return render_citation_block([get(k) for k in keys])
