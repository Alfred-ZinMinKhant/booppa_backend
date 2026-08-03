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

Shelf life: re-verify this table against the MAS website quarterly. The entries
below carry `verified_on` for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional, Sequence, Tuple

from app.services.mas_licence import (
    MAS_LICENCE_TYPES,
    REGIME_BANK_NOTICES,
    normalise_licence,
    outsourcing_regime,
)

# Last human re-verification of every entry against the MAS website.
REGISTRY_VERIFIED_ON = date(2026, 8, 3)

_ALL_LICENCES = frozenset(MAS_LICENCE_TYPES)
_NON_BANK = frozenset(k for k in MAS_LICENCE_TYPES if k not in {"bank", "merchant_bank"})


@dataclass(frozen=True)
class MasInstrument:
    key: str
    ids: Tuple[str, ...]
    title: str
    instrument_type: str  # "notice" | "guidelines"
    binding: bool  # notices bind; guidelines are guidance
    status: str  # "current" | "superseded"
    applies_to: frozenset
    citation: str
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


MAS_INSTRUMENTS: Dict[str, MasInstrument] = {
    "fsm_n06": MasInstrument(
        key="fsm_n06",
        ids=("FSM-N06",),
        title="Cyber Hygiene",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=_ALL_LICENCES,
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
    "notice_655": MasInstrument(
        key="notice_655",
        ids=("655",),
        title="Cyber Hygiene (cancelled)",
        instrument_type="notice",
        binding=False,
        status="superseded",
        applies_to=_ALL_LICENCES,
        citation="MAS Notice 655 — Cyber Hygiene (cancelled 10 May 2024)",
        superseded_by="fsm_n06",
        superseded_on=date(2024, 5, 10),
    ),
    "fsm_n05": MasInstrument(
        key="fsm_n05",
        ids=("FSM-N05", "644"),
        title="Technology Risk Management",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=_ALL_LICENCES,
        citation="MAS Notice FSM-N05 (formerly 644) — Technology Risk Management",
        key_obligations=(
            "Notify MAS within 1 hour of discovering a relevant incident.",
            "Submit a root cause and impact analysis to MAS within 14 days.",
            "Recover a critical system within 4 hours of an unscheduled outage.",
            "Total unscheduled downtime of a critical system not to exceed 4 hours "
            "in any rolling 12-month period.",
        ),
    ),
    "notice_658": MasInstrument(
        key="notice_658",
        ids=("658",),
        title="Outsourcing (Banks)",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"bank"}),
        citation="MAS Notice 658 — Outsourcing (Banks)",
        binding_from=date(2024, 12, 11),
        key_obligations=(
            "Maintain a register of all outsourcing arrangements in the format "
            "specified by MAS.",
            "Submit the register to MAS on request and keep it current.",
            "Board or senior management approval of material outsourcing arrangements.",
            "Assess and record sub-outsourcing by the service provider.",
        ),
    ),
    "notice_1121": MasInstrument(
        key="notice_1121",
        ids=("1121",),
        title="Outsourcing (Merchant Banks)",
        instrument_type="notice",
        binding=True,
        status="current",
        applies_to=frozenset({"merchant_bank"}),
        citation="MAS Notice 1121 — Outsourcing (Merchant Banks)",
        binding_from=date(2024, 12, 11),
        key_obligations=(
            "Maintain a register of all outsourcing arrangements in the format "
            "specified by MAS.",
            "Submit the register to MAS on request and keep it current.",
            "Board or senior management approval of material outsourcing arrangements.",
            "Assess and record sub-outsourcing by the service provider.",
        ),
    ),
    "outsourcing_guidelines_nonbank": MasInstrument(
        key="outsourcing_guidelines_nonbank",
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
    "trm_guidelines": MasInstrument(
        key="trm_guidelines",
        ids=(),
        title="Technology Risk Management Guidelines",
        instrument_type="guidelines",
        binding=False,
        status="current",
        applies_to=_ALL_LICENCES,
        citation="MAS Technology Risk Management Guidelines",
        key_obligations=(
            "Board and senior management oversight of technology risk.",
            "Technology risk management framework covering identification, "
            "assessment, treatment and monitoring.",
            "Independent audit and regular vulnerability assessment and penetration "
            "testing of critical systems.",
        ),
    ),
    "bcm_guidelines": MasInstrument(
        key="bcm_guidelines",
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


def superseded_warning(key: str) -> Optional[str]:
    """A sentence naming an instrument as cancelled, or None if it is current."""
    inst = get(key)
    if inst.is_current:
        return None
    replacement = MAS_INSTRUMENTS.get(inst.superseded_by or "")
    on = inst.superseded_on.strftime("%d %B %Y") if inst.superseded_on else "an earlier date"
    tail = f" and replaced by {replacement.citation}" if replacement else ""
    return (
        f"{inst.citation.split(' — ')[0]} was cancelled on {on}{tail}. It is no "
        f"longer in force and must not be cited as a current obligation."
    )


def citation_block(keys: Sequence[str]) -> str:
    """Render instruments as a prompt-injectable block.

    Superseded instruments are included *only* with their cancellation stated, so
    the model has an explicit correction rather than a gap its training data
    fills with the old notice number.
    """
    lines: list[str] = []
    for key in keys:
        inst = get(key)
        if not inst.is_current:
            lines.append(f"- DO NOT CITE AS CURRENT: {superseded_warning(key)}")
            continue
        nature = (
            "BINDING NOTICE (legally enforceable)"
            if inst.binding
            else "GUIDELINES (guidance, not legally binding — describe as such)"
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
