"""
Booppa MAS TRM Suite — Document Pack Generator (DeepSeek)

The TRM Suite previously produced a 13-domain status tracker and a monthly board
report, and nothing else. This module generates the artefacts a MAS-regulated
entity must actually hold on file — the TRM equivalent of csp_doc_generator.py.

Seven documents (see TRM_DOCUMENT_CATALOG):
  1. Technology Risk Governance Policy
  2. Cyber Hygiene Compliance Attestation      [clause-validated, licence-branched]
  3. Outsourcing Risk Register                                    [licence-branched]
  4. Incident Response Plan (TRM Notice timelines)                [licence-branched]
  5. Business Continuity & Disaster Recovery Plan
  6. IT Audit & VAPT Tracking Log
  7. Access Control & Authentication Policy

Three properties are load-bearing and must survive refactoring:

* **Every generator takes exactly one argument, `fn(ctx)`.** The CSP catalog
  dispatches on generator arity and shipped 3-of-8 packs to paying customers for
  months as a result (csp_doc_generator.py:803). A uniform signature makes that
  bug class unrepresentable here.

* **The outsourcing branch is decided in Python, never by the model.** Two
  disjoint prompts and two disjoint schemas; the model is never shown both
  regimes and asked to pick. A bank handed a Guidelines-format register is not a
  slightly worse document — it is signed evidence that the entity has
  misidentified its own regulatory regime.

* **The Cyber Hygiene Attestation is checked programmatically after generation.**
  It is a signed representation to MAS. If the model returns a generic "cyber
  security" narrative instead of clause-by-clause coverage of the Notice that
  actually binds this entity, the document fails rather than ships.

* **No document names a Notice the registry did not resolve for that licence.**
  Every entity type has its own TRM and Cyber Hygiene Notices; there is no
  house default. A document whose instrument is unmapped is withheld, never
  generated against the bank Notice.

Cost discipline (see the deepseek prompt-cost invariant): never pass
Report.assessment_data, never pass raw TrmEvidence rows, and truncate every
gap_analysis. _ctx_block is size-capped with a runtime check.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.mas_licence import (
    is_bank_class,
    licence_label,
    outsourcing_regime,
    REGIME_BANK_NOTICES,
)
from app.services import mas_notice_registry as registry

logger = logging.getLogger(__name__)

# Bump when the *structure* of any generated document changes, so packs held by
# existing customers can be identified as outdated (same contract as
# COVER_SHEET_SCHEMA_VERSION).
TRM_DOC_SCHEMA_VERSION = 1

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MAX_TOKENS = 4096
_IN = 0.014
_OUT = 0.280

# Hard ceiling on the context block handed to every prompt. 13 domains of
# free-text gap analysis will otherwise grow without bound as customers fill the
# workspace in, and max_tokens caps the *response*, not the request.
_CTX_MAX_CHARS = 8000
_GAP_MAX_CHARS = 300

# The convention for anything the customer has not evidenced. The model must
# never invent a plausible-sounding control claim in a document that will be
# signed and handed to a regulator.
UNEVIDENCED = "NOT YET EVIDENCED — [CUSTOMISE]"


# ── CONTEXT ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ControlSnapshot:
    domain: str
    control_ref: Optional[str]
    status: str
    risk_rating: Optional[str]
    gap_analysis: Optional[str]
    evidence_count: int = 0
    tested_count: int = 0
    latest_tested_at: Optional[str] = None

    @property
    def is_tested(self) -> bool:
        return self.tested_count > 0


@dataclass(frozen=True)
class TrmDocContext:
    legal_name: str
    uen: str
    licence_type: Optional[str]  # None = not confirmed. Never a default.
    sector: Optional[str]
    plan_label: str
    controls: Tuple[ControlSnapshot, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def licence_label(self) -> str:
        return licence_label(self.licence_type)

    @property
    def licence_known(self) -> bool:
        return self.licence_type is not None

    @property
    def evidence_documented(self) -> int:
        return sum(c.evidence_count for c in self.controls)

    @property
    def evidence_tested(self) -> int:
        return sum(c.tested_count for c in self.controls)


def build_trm_context(
    org,
    user,
    controls: List[Dict[str, Any]],
    db=None,
    *,
    override_company: Optional[str] = None,
) -> TrmDocContext:
    """Assemble the generation context from ORM rows.

    `controls` takes the dict shape already produced by
    tasks.run_suite_trm_baseline_for_user._fetch_controls, so the pack and the
    tracker are generated from identical inputs and cannot disagree.

    The entity name comes from display_legal_name — the same helper the baseline
    uses — not from User.legal_name directly, which may be a stale cache from a
    different purchase (see the legal-name hint staleness invariant). The UEN goes
    through the same trust gate: a cached registration number whose provenance
    does not match this pack's subject belongs to a *different* entity, and
    stamping it on a MAS attestation cover is worse than printing nothing.
    """
    from app.services.evidence_enricher import display_legal_name, trusted_cached_uen

    # display_legal_name already owns the whole fallback chain (resolved name →
    # hint → raw company → neutral placeholder). Re-implementing the tail here is
    # how the raw signup string sneaks back past the resolver.
    legal_name = display_legal_name(user, db, company_hint=override_company) or "[Entity Name]"
    plan = getattr(user, "plan", "") or ""
    snapshots = tuple(
        ControlSnapshot(
            domain=c.get("domain") or "Unknown Domain",
            control_ref=c.get("control_ref"),
            status=c.get("status") or "not_started",
            risk_rating=c.get("risk_rating"),
            gap_analysis=(c.get("gap_analysis") or "")[:_GAP_MAX_CHARS] or None,
            evidence_count=int(c.get("evidence_count") or 0),
            tested_count=int(c.get("tested_count") or 0),
            latest_tested_at=c.get("latest_tested_at"),
        )
        for c in (controls or [])
    )
    return TrmDocContext(
        legal_name=legal_name,
        uen=trusted_cached_uen(user, company_hint=override_company) or "[UEN]",
        licence_type=getattr(org, "mas_licence_type", None) if org else None,
        sector=getattr(org, "sector", None) if org else None,
        plan_label="Pro Suite" if plan == "pro_suite" else "Standard Suite",
        controls=snapshots,
    )


def _ctx_block(ctx: TrmDocContext) -> str:
    """Render the context every prompt shares.

    Size-capped: an over-long block is truncated with a logged warning rather
    than silently billed. Truncation here is safe — the control table is ordered
    and the tail is the least material part — but it must never be silent, which
    is how the previous prompt-cost overrun went unnoticed.
    """
    lines = [
        "=== ASSESSED ENTITY ===",
        f"Legal name:        {ctx.legal_name}",
        f"UEN:               {ctx.uen}",
        f"MAS licence class: {ctx.licence_label}",
        f"Subscription:      {ctx.plan_label}",
        f"Document date:     {ctx.generated_at.strftime('%d %B %Y')}",
        "",
        "=== TRM CONTROL BASELINE (13 MAS TRM domains) ===",
        f"Evidence items documented: {ctx.evidence_documented}",
        f"Evidence items TESTED:     {ctx.evidence_tested}",
        "",
    ]
    for c in ctx.controls:
        bits = [f"- {c.domain} [{c.control_ref or 'n/a'}] status={c.status}"]
        if c.risk_rating:
            bits.append(f"risk={c.risk_rating}")
        bits.append(f"evidence={c.evidence_count} tested={c.tested_count}")
        if c.latest_tested_at:
            bits.append(f"last_tested={c.latest_tested_at}")
        lines.append(" ".join(bits))
        if c.gap_analysis:
            # Truncated here as well as in build_trm_context: a ControlSnapshot
            # built directly (tests, demo harness) would otherwise carry an
            # unbounded gap narrative straight into the prompt.
            lines.append(f"    gap: {c.gap_analysis[:_GAP_MAX_CHARS]}")

    block = "\n".join(lines)
    if len(block) > _CTX_MAX_CHARS:
        logger.warning(
            "[TRMDoc] context block %d chars exceeds cap %d for %s — truncating",
            len(block), _CTX_MAX_CHARS, ctx.legal_name,
        )
        block = block[:_CTX_MAX_CHARS] + "\n[context truncated]"
    return block


# ── MODEL CALLS ──────────────────────────────────────────────────────────────

_SYSTEM_BASE = """You are a Singapore technology risk and regulatory compliance
specialist advising MAS-regulated financial institutions. You have deep working
knowledge of:
- MAS Technology Risk Management Guidelines
- The MAS Technology Risk Management Notices issued under the FSMA in May 2024,
  which are per-licence-class: FSM-N05 (banks, formerly Notice 644), FSM-N11
  (merchant banks), FSM-N03 (insurers), FSM-N21 (capital markets)
- The MAS Cyber Hygiene Notices, likewise per-licence-class: FSM-N06 (banks,
  which replaced the cancelled Notice 655 on 10 May 2024), FSM-N12 (merchant
  banks), FSM-N04 (insurers), FSM-N22 (capital markets), FSM-N14 (payment
  services and digital payment token licensees)
- MAS Notice 658 (Banks) and MAS Notice 1121 (Merchant Banks) on Outsourcing,
  binding since 11 December 2024
- MAS Guidelines on Outsourcing (Financial Institutions other than Banks)
- MAS Guidelines on Business Continuity Management

Hard rules you never break:
- The Notices are legally binding. The Guidelines are guidance — describe them as
  guidance, never as binding obligations.
- Never cite MAS Notice 655. It was cancelled on 10 May 2024. Cite the Cyber
  Hygiene Notice for this entity's licence class, as named in the APPLICABLE MAS
  INSTRUMENTS block of the request.
- Never name a MAS Notice that is not in that block. The Notice that binds an
  entity is determined by its licence class, and asserting the wrong one in a
  document the entity will sign is a false statement of its obligations.
- Never assert that a control is implemented, tested, or evidenced unless the
  supplied context says so. Where the entity has no evidence, write exactly
  "%s" and nothing more reassuring. A plausible-sounding
  unsupported claim in a document signed and given to a regulator is the worst
  possible output.
- Write in Singapore English, suitable for board and regulator review.

Return ONLY the document content. No preamble, no meta-commentary.""" % UNEVIDENCED


def _deepseek_key() -> str:
    """The API key, from settings first.

    `os.environ` alone is what CSP does, and it is right in ECS where the key is
    a real task-definition variable — but pydantic-settings reads `.env` without
    exporting it, so any local front door (the demo script, a shell) died on a
    bare `KeyError: 'DEEPSEEK_API_KEY'` while the key was sitting in `.env`.
    """
    from app.core.config import settings

    key = getattr(settings, "DEEPSEEK_API_KEY", None) or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not configured — TRM document generation cannot run."
        )
    return key


def _call_text(system: str, user: str) -> Tuple[str, int, int]:
    """Free-text generation (narrative policies and plans)."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("pip install openai")
    client = OpenAI(
        api_key=_deepseek_key(),
        base_url=DEEPSEEK_BASE_URL,
        timeout=120.0,
    )
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0.15,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (
        resp.choices[0].message.content or "",
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )


def _call_json(system: str, user: str) -> Tuple[Dict[str, Any], int, int]:
    """Structured generation (registers and logs).

    Registers must render as real tables in the PDF. Asking for free text and
    parsing markdown bullets back into rows is how a register turns into prose
    that no inspector can read down a column of.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("pip install openai")
    client = OpenAI(
        api_key=_deepseek_key(),
        base_url=DEEPSEEK_BASE_URL,
        timeout=120.0,
    )
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0.15,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    return (
        json.loads(raw),
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )


def _call_with_retry(fn: Callable, *args, attempts: int = 2):
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            last = e
            logger.warning("[TRMDoc] %s attempt %d/%d failed: %s",
                           getattr(fn, "__name__", "call"), i + 1, attempts, e)
    raise last  # type: ignore[misc]


# ── 1. TECHNOLOGY RISK GOVERNANCE POLICY ─────────────────────────────────────

def gen_governance_policy(ctx: TrmDocContext) -> Tuple[str, int, int]:
    trm_inst = registry.technology_risk_instrument(ctx.licence_type)
    notice_id = trm_inst.ids[0]
    cites = registry.citation_block(["trm_guidelines", trm_inst.key])
    prompt = f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Draft a Technology Risk Governance Policy for {ctx.legal_name}.

1. PURPOSE, SCOPE AND REGULATORY BASIS
   State that the policy gives effect to the MAS Technology Risk Management
   Guidelines and the entity's obligations under MAS Notice {notice_id}. Be
   explicit that the TRM Guidelines are guidance and {notice_id} is a binding
   notice.
2. BOARD AND SENIOR MANAGEMENT ACCOUNTABILITY
   Board responsibilities, the risk appetite statement, reporting cadence, and
   the named roles to be appointed ({UNEVIDENCED} where unappointed).
3. THREE LINES OF DEFENCE for technology risk at this entity.
4. TECHNOLOGY RISK MANAGEMENT FRAMEWORK
   Identification, assessment, treatment, monitoring; the risk register; the
   criteria for escalating a technology risk to the Board.
5. THE 13 TRM DOMAINS — a subsection per domain listing the domain owner, the
   current baseline status from the context above, and the next action. Use the
   real statuses given; do not upgrade a "not_started" domain to a claim.
6. POLICY REVIEW, EXCEPTIONS AND WAIVERS.
7. APPROVAL — board approval reference, effective date, review date.

Reference specific TRM Guidelines sections where relevant.
Use Markdown headings (##) and tables."""
    return _call_text(_SYSTEM_BASE, prompt)


# ── 2. CYBER HYGIENE COMPLIANCE ATTESTATION (per-licence Notice) ────────────

_CYBER_HYGIENE_MIN_MENTIONS = 3


def _validate_cyber_hygiene(content: str, inst) -> List[str]:
    """Return the reasons this attestation is unacceptable, empty if it passes.

    This exists because the failure mode is silent and expensive: the model
    produces a fluent, professional, generic "cyber security policy" that never
    engages the entity's actual Cyber Hygiene Notice clause by clause, and it
    gets signed. A word count and a plausible tone cannot distinguish that from
    the real thing; the clause anchors can.

    ``inst`` is the resolved instrument for this entity's licence class (e.g.
    FSM-N14 for a payment institution, FSM-N06 for a bank) — never hardcoded.
    """
    problems: List[str] = []
    notice_id = inst.ids[0]
    if content.count(notice_id) < _CYBER_HYGIENE_MIN_MENTIONS:
        problems.append(
            f"cites {notice_id} only {content.count(notice_id)} time(s), "
            f"minimum {_CYBER_HYGIENE_MIN_MENTIONS}"
        )
    missing = [a for a in inst.clause_anchors if a.lower() not in content.lower()]
    if missing:
        problems.append("missing clause coverage: " + ", ".join(missing))
    # A bare "Notice 655" that is not immediately qualified as cancelled is a
    # live citation of a dead instrument.
    lowered = content.lower()
    idx = lowered.find("notice 655")
    while idx != -1:
        window = lowered[max(0, idx - 200): idx + 200]
        if not any(w in window for w in ("cancel", "supersed", "replaced", "no longer")):
            problems.append("cites cancelled Notice 655 without qualification")
            break
        idx = lowered.find("notice 655", idx + 1)
    return problems


def _cyber_hygiene_prompt(ctx: TrmDocContext, inst, *, strict: bool) -> str:
    notice_id = inst.ids[0]
    # Notice 655 (cancelled) was bank-specific — other licence classes had their
    # own predecessor notice (insurers: 132, credit/charge card: 655A, finance
    # companies: 834, ...). We have not verified those numbers, so we cite the
    # cancellation only for the licence it actually applied to rather than
    # attaching the bank predecessor to every entity type. See email to Alfred,
    # item 1, for the follow-up to source and add the others properly.
    citation_keys = [inst.key] + (["notice_655"] if inst.key in ("fsm_n05", "fsm_n06") else [])
    cites = registry.citation_block(citation_keys)
    anchors = "\n".join(f"   {i+1}. {a}" for i, a in enumerate(inst.clause_anchors))
    extra = ""
    if strict:
        extra = f"""

THE PREVIOUS ATTEMPT WAS REJECTED. It read as a generic cyber security document.
This attempt MUST:
- contain the literal string "{notice_id}" at least {_CYBER_HYGIENE_MIN_MENTIONS} times;
- contain one table row per clause heading, using the heading text verbatim;
- contain no generic best-practice filler that is not tied to a clause."""
    return f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Draft a Cyber Hygiene Compliance Attestation for {ctx.legal_name} against MAS
Notice {notice_id} specifically. This is a formal attestation instrument, not a
policy and not a general cyber security overview.

STRUCTURE
1. ATTESTATION STATEMENT — the entity, its UEN, its MAS licence class
   ({ctx.licence_label}), the attestation period, and the authorised
   representative who will sign.
2. REGULATORY BASIS — one paragraph stating that {notice_id} is a binding MAS
   Notice on cyber hygiene for this licence class, and that this attestation
   addresses each of its requirements in turn.
3. CLAUSE-BY-CLAUSE ATTESTATION — a table with columns:
   Clause | Requirement under {notice_id} | Implementation at this entity | Evidence reference | Attested status
   One row for EACH of the following clause headings, using the heading text
   verbatim in the Clause column:
{anchors}
   Attested status is one of: IMPLEMENTED AND EVIDENCED / IMPLEMENTED, EVIDENCE
   PENDING / NOT IMPLEMENTED. Where the context above shows no evidence for the
   relevant domain, Evidence reference must read "{UNEVIDENCED}" and the status
   must NOT be "IMPLEMENTED AND EVIDENCED".
4. EXCEPTIONS AND REMEDIATION PLAN — every clause not fully implemented, with an
   owner and a target date placeholder.
5. SIGN-OFF BLOCK — name, designation, date.

Do not write a general cyber security narrative. Every paragraph must attach to a
clause of {notice_id}.{extra}"""


def gen_cyber_hygiene_attestation(ctx: TrmDocContext) -> Tuple[str, int, int]:
    # Raises if licence_type is unmapped — the catalog gate (requires_licence)
    # must keep this from being called with ctx.licence_known == False. See
    # TRM_DOCUMENT_CATALOG below.
    inst = registry.cyber_hygiene_instrument(ctx.licence_type)
    content, in_tok, out_tok = _call_text(_SYSTEM_BASE, _cyber_hygiene_prompt(ctx, inst, strict=False))
    problems = _validate_cyber_hygiene(content, inst)
    if not problems:
        return content, in_tok, out_tok

    logger.warning("[TRMDoc] %s attestation rejected (%s) — retrying stricter",
                   inst.ids[0], "; ".join(problems))
    content2, in2, out2 = _call_text(_SYSTEM_BASE, _cyber_hygiene_prompt(ctx, inst, strict=True))
    problems2 = _validate_cyber_hygiene(content2, inst)
    if problems2:
        # Fail the document rather than ship a generic attestation. An empty slot
        # in the pack is recoverable; a signed generic attestation presented to
        # MAS as coverage of the wrong Notice is not.
        raise ValueError(
            f"Cyber Hygiene Attestation failed {inst.ids[0]} validation after retry: "
            + "; ".join(problems2)
        )
    return content2, in_tok + in2, out_tok + out2


# ── 3. OUTSOURCING RISK REGISTER (licence-branched) ──────────────────────────

_BANK_REGISTER_COLUMNS = [
    "Arrangement Ref", "Service Provider", "Group or Third Party", "Jurisdiction",
    "Service Description", "Material (Y/N)", "Criticality", "Sub-outsourcing",
    "Commencement Date", "Expiry Date", "Last Risk Assessment", "Board Approval Ref",
]

_NON_BANK_REGISTER_COLUMNS = [
    "Arrangement Ref", "Service Provider", "Jurisdiction", "Service Description",
    "Material (Y/N)", "Risk Assessment Date", "Due Diligence Completed",
    "Audit / MAS Access Rights in Contract", "Review Date", "Accountable Owner",
]


def _register_schema_hint(columns: List[str]) -> str:
    cols = ", ".join(f'"{c}"' for c in columns)
    return (
        '{"header": "<one paragraph naming the governing instrument>", '
        '"columns": [' + cols + '], '
        '"rows": [{"' + columns[0] + '": "...", ...}], '
        '"notes": ["<completion instruction>", ...]}'
    )


def gen_outsourcing_register(ctx: TrmDocContext) -> Tuple[Dict[str, Any], int, int]:
    """The licence branch. Raises on an unknown licence — deliberately.

    outsourcing_regime() raises rather than defaulting, and nothing here catches
    it: the pack builder gates this document and asks the customer. Adding a
    fallback regime is the single change that would make this product dangerous.
    """
    regime = outsourcing_regime(ctx.licence_type)
    instruments = registry.outsourcing_instruments(ctx.licence_type)
    inst = instruments[0]
    cites = registry.citation_block([i.key for i in instruments])

    if regime == REGIME_BANK_NOTICES:
        assert is_bank_class(ctx.licence_type)
        columns = _BANK_REGISTER_COLUMNS
        regime_rules = f"""The governing instrument is {inst.citation}, a BINDING
MAS Notice in force since 11 December 2024. It requires a register of ALL
outsourcing arrangements in the format specified by MAS, submitted to MAS on
request. The header paragraph must name {inst.citation} and no other outsourcing
instrument. Do not mention the Guidelines on Outsourcing (Financial Institutions
other than Banks) — they do not apply to this entity."""
    else:
        columns = _NON_BANK_REGISTER_COLUMNS
        regime_rules = f"""The governing instrument is the {inst.citation}. These
are GUIDELINES — guidance, not a binding notice — and the header paragraph must
say so plainly. Do not cite MAS Notice 658 or MAS Notice 1121; those bind banks
and merchant banks respectively and do not apply to this entity
({ctx.licence_label})."""

    prompt = f"""{_ctx_block(ctx)}

=== GOVERNING OUTSOURCING INSTRUMENT ===
{cites}

{regime_rules}

Produce an Outsourcing Risk Register template for {ctx.legal_name}.

The entity has not supplied its list of outsourcing arrangements. Produce the
register structured and ready to complete, with 3 to 5 illustrative rows that are
clearly marked as examples for the entity's own arrangements — every factual
cell in an example row must read "{UNEVIDENCED}" rather than inventing a supplier
name, a date, or an approval reference.

Return JSON with exactly this shape:
{_register_schema_hint(columns)}

"columns" must be exactly the list given above, in that order.
"notes" must include the review cadence, who maintains the register, and what
triggers a re-assessment of a material arrangement."""

    data, in_tok, out_tok = _call_json(_SYSTEM_BASE, prompt)
    # The column set is ours, not the model's — a register whose columns drifted
    # is not the instrument's specified format.
    data["columns"] = columns
    data["regime"] = regime
    data["instrument"] = inst.citation
    return data, in_tok, out_tok


# ── 4. INCIDENT RESPONSE PLAN ────────────────────────────────────────────────

def gen_incident_response_plan(ctx: TrmDocContext) -> Tuple[str, int, int]:
    trm_inst = registry.technology_risk_instrument(ctx.licence_type)
    notice_id = trm_inst.ids[0]
    cites = registry.citation_block([trm_inst.key, "trm_guidelines"])
    prompt = f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Draft an Incident Response Plan for {ctx.legal_name}.

The MAS notification timelines below are binding under MAS Notice {notice_id}
and must appear in the document explicitly, as numbers, not as "promptly":
- Notify MAS within 1 HOUR of discovering a relevant incident.
- Submit a root cause and impact analysis to MAS within 14 DAYS.
- Recover a critical system within 4 HOURS of an unscheduled outage.
- Total unscheduled downtime of a critical system must not exceed 4 HOURS in any
  rolling 12-month period.

STRUCTURE
1. SCOPE AND DEFINITIONS — what constitutes a relevant incident under
   {notice_id}, severity tiers, and what is explicitly out of scope.
2. ROLES — incident commander, MAS notification owner, communications, technical
   lead. Name the roles; use {UNEVIDENCED} for unappointed individuals.
3. DETECTION AND TRIAGE — how the 1-hour clock starts, and who is authorised to
   declare an incident. Be explicit that the clock runs from DISCOVERY.
4. THE MAS NOTIFICATION PROCEDURE — a step-by-step runbook for the first hour,
   presented as a table with elapsed-time markers (T+0, T+15min, T+45min, T+1h),
   including what must be in the initial notification when facts are incomplete.
5. CONTAINMENT, ERADICATION, RECOVERY — against the 4-hour recovery objective.
6. THE 14-DAY ROOT CAUSE ANALYSIS — required contents and the approval path.
7. DOWNTIME LEDGER — how cumulative unscheduled downtime is tracked against the
   4-hour rolling 12-month cap, and the escalation when the entity approaches it.
8. POST-INCIDENT REVIEW AND PLAN TESTING — including the record that must be kept
   to show the plan was TESTED, not merely documented.

Use Markdown headings (##) and tables."""
    return _call_text(_SYSTEM_BASE, prompt)


# ── 5. BCM / DR PLAN ─────────────────────────────────────────────────────────

def gen_bcm_dr_plan(ctx: TrmDocContext) -> Tuple[str, int, int]:
    trm_inst = registry.technology_risk_instrument(ctx.licence_type)
    notice_id = trm_inst.ids[0]
    cites = registry.citation_block(["bcm_guidelines", trm_inst.key])
    prompt = f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Draft a Business Continuity and Disaster Recovery Plan for {ctx.legal_name}.

Note the instrument split precisely: business continuity management is addressed
by the MAS Guidelines on Business Continuity Management (guidance), while the
4-hour recovery objective and the 4-hour rolling 12-month unscheduled downtime
cap for critical systems are binding under MAS Notice {notice_id}.

STRUCTURE
1. SCOPE, OBJECTIVES AND GOVERNANCE — board accountability for continuity
   preparedness and the annual attestation.
2. CRITICAL BUSINESS SERVICES — a table: Service | Owner | Dependencies |
   RTO | RPO | Maximum tolerable downtime. Present RTO/RPO as fields the entity
   must set; do not invent values. Where the service is not yet identified use
   {UNEVIDENCED}.
3. BUSINESS IMPACT ANALYSIS methodology and cadence.
4. RECOVERY STRATEGIES — per critical service, including the technology recovery
   (DR site / cloud region failover) and the manual workaround.
5. INVOCATION — who declares, the decision tree, and the notification cascade.
6. DEPENDENCY AND CONCENTRATION RISK — third parties and cloud regions.
7. TESTING PROGRAMME — this section is the point of the document. State plainly
   that MAS treats an untested plan as an aspiration rather than a control, and
   set out the test types (walkthrough, tabletop, full failover), the cadence,
   the evidence to be retained per test, and the board reporting of results.
   The entity's current baseline shows {ctx.evidence_tested} tested evidence
   items against {ctx.evidence_documented} documented — reflect that honestly.
8. PLAN MAINTENANCE AND APPROVAL.

Use Markdown headings (##) and tables."""
    return _call_text(_SYSTEM_BASE, prompt)


# ── 6. IT AUDIT & VAPT TRACKING LOG ──────────────────────────────────────────

_VAPT_COLUMNS = [
    "Ref", "Activity Type", "Scope / System", "Scheduled Date", "Performed By",
    "Independence", "Status", "Findings (Critical/High/Med/Low)",
    "Remediation Owner", "Target Close Date", "Evidence Reference",
]


def gen_it_audit_vapt_log(ctx: TrmDocContext) -> Tuple[Dict[str, Any], int, int]:
    cites = registry.citation_block(["trm_guidelines", "fsm_n06"])
    prompt = f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Produce an IT Audit and VAPT (Vulnerability Assessment and Penetration Testing)
Tracking Log for {ctx.legal_name}.

This is the record an inspector asks for to distinguish a tested control from a
documented one. It must be structured so that an empty cell is visibly an open
item, not an implied pass.

Activity types to schedule: internal IT audit, external IT audit, vulnerability
assessment, penetration test, source code review, red team exercise, and the
security patch review cycle. Do NOT name a MAS Notice number anywhere in this
log: it is the one document in the pack generated without a confirmed licence
class, so the Notice that binds this entity is not known here.

The entity has not supplied completed audit history. Produce the log with the
forward schedule rows for a 12-month cycle. Every cell recording a past result —
Performed By, Status, Findings, Evidence Reference — must read "{UNEVIDENCED}".
Do not invent a finding count or a clean result.

Return JSON with exactly this shape:
{_register_schema_hint(_VAPT_COLUMNS)}

"notes" must cover: the independence requirement for the tester, the minimum
cadence expected under the TRM Guidelines, the remediation SLA by severity, and
the retention of evidence per activity."""
    data, in_tok, out_tok = _call_json(_SYSTEM_BASE, prompt)
    data["columns"] = _VAPT_COLUMNS
    return data, in_tok, out_tok


# ── 7. ACCESS CONTROL & AUTHENTICATION POLICY ────────────────────────────────

def gen_access_control_policy(ctx: TrmDocContext) -> Tuple[str, int, int]:
    ch_inst = registry.cyber_hygiene_instrument(ctx.licence_type)
    notice_id = ch_inst.ids[0]
    cites = registry.citation_block([ch_inst.key, "trm_guidelines"])
    prompt = f"""{_ctx_block(ctx)}

=== APPLICABLE MAS INSTRUMENTS ===
{cites}

Draft an Access Control and Authentication Policy for {ctx.legal_name}.

Two {notice_id} requirements are binding and must be addressed by name:
- Administrative accounts must be secured against unauthorised access.
- Multi-factor authentication is required for administrative access to critical
  systems, and for online access by customers to their accounts.

STRUCTURE
1. SCOPE AND PRINCIPLES — least privilege, need to know, segregation of duties.
2. IDENTITY LIFECYCLE — joiner, mover, leaver; the leaver revocation SLA.
3. PRIVILEGED ACCESS MANAGEMENT — the {notice_id} administrative accounts
   requirement: inventory, approval, vaulting, session recording, break-glass
   procedure and its after-the-fact review.
4. AUTHENTICATION STANDARDS — the {notice_id} multi-factor authentication
   requirement, split into administrative access to critical systems and customer
   online access. Cover accepted factor types and the treatment of SMS OTP.
5. ACCESS REVIEW — cadence by privilege tier, reviewer, and the evidence retained.
6. THIRD PARTY AND SERVICE ACCOUNT ACCESS.
7. LOGGING, MONITORING AND ALERTING of access events.
8. EXCEPTIONS, ENFORCEMENT AND POLICY REVIEW.

Use Markdown headings (##) and tables."""
    return _call_text(_SYSTEM_BASE, prompt)


# ── CATALOG ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrmDocSpec:
    doc_type: str
    title: str
    fn: Callable[[TrmDocContext], Tuple[Any, int, int]]
    mode: str                      # "text" | "json"
    requires_instrument: Optional[str]  # instrument that must resolve, or None
    notices: Tuple[str, ...]       # registry keys, for provenance on the PDF
    domain: str                    # owning MAS TRM domain
    verification_critical: bool    # drives the RED draft banner


# Every generator takes exactly one argument. Do not add a second parameter to
# any of these — see the module docstring.
#
# "trm_notice", "cyber_hygiene_notice" and "outsourcing" are sentinels, not
# registry keys — the actual instrument (FSM-N05 for a bank, FSM-N14 for a
# payment institution, ...) depends on ctx.licence_type and is resolved through
# _resolve_instrument(). Do not put a literal "fsm_n05" or "fsm_n06" key back
# here — that is the bug this catalog fixes.
#
# `requires_instrument` is the gate. It is deliberately not a bare
# "requires_licence" boolean: the Notice families do not have the same coverage,
# so "is the licence known?" is the wrong question. An MPI has a confirmed
# Cyber Hygiene Notice (FSM-N14) but no confirmed TRM Notice, and must therefore
# receive the attestation and access control policy while the three TRM-Notice
# documents gate. Ask the registry per document, not per account.
_TRM_NOTICE = "trm_notice"
_CYBER_HYGIENE_NOTICE = "cyber_hygiene_notice"
_OUTSOURCING = "outsourcing"

TRM_DOCUMENT_CATALOG: Tuple[TrmDocSpec, ...] = (
    TrmDocSpec(
        "trm_governance_policy", "Technology Risk Governance Policy",
        gen_governance_policy, "text", _TRM_NOTICE,
        ("trm_guidelines", _TRM_NOTICE), "Technology Risk Governance", False,
    ),
    TrmDocSpec(
        "cyber_hygiene_attestation",
        "Cyber Hygiene Compliance Attestation",
        gen_cyber_hygiene_attestation, "text", _CYBER_HYGIENE_NOTICE,
        (_CYBER_HYGIENE_NOTICE,), "Cyber Security", True,
    ),
    TrmDocSpec(
        "outsourcing_risk_register", "Outsourcing Risk Register",
        gen_outsourcing_register, "json", _OUTSOURCING,
        (), "IT Outsourcing and Vendor Management", True,
    ),
    TrmDocSpec(
        "incident_response_plan", "Incident Response Plan",
        gen_incident_response_plan, "text", _TRM_NOTICE,
        (_TRM_NOTICE,), "Incident Management", True,
    ),
    TrmDocSpec(
        "bcm_dr_plan", "Business Continuity and Disaster Recovery Plan",
        gen_bcm_dr_plan, "text", _TRM_NOTICE,
        ("bcm_guidelines", _TRM_NOTICE), "Business Continuity and Disaster Recovery",
        False,
    ),
    TrmDocSpec(
        "it_audit_vapt_log", "IT Audit and VAPT Tracking Log",
        gen_it_audit_vapt_log, "json", None,
        ("trm_guidelines",), "IT Audit", False,
    ),
    TrmDocSpec(
        "access_control_policy", "Access Control and Authentication Policy",
        gen_access_control_policy, "text", _CYBER_HYGIENE_NOTICE,
        ("trm_guidelines", _CYBER_HYGIENE_NOTICE), "Authentication and Access Management", False,
    ),
)

TRM_DOC_TYPES: Tuple[str, ...] = tuple(s.doc_type for s in TRM_DOCUMENT_CATALOG)


def _resolve_instrument(kind: str, licence_type: Optional[str]):
    """Sentinel → the instrument that actually binds this entity.

    Raises ValueError when nothing is mapped. Callers treat that as "gate this
    document", never as "fall back to the bank Notice".
    """
    if kind == _TRM_NOTICE:
        return registry.technology_risk_instrument(licence_type)
    if kind == _CYBER_HYGIENE_NOTICE:
        return registry.cyber_hygiene_instrument(licence_type)
    if kind == _OUTSOURCING:
        return registry.outsourcing_instruments(licence_type)[0]
    return registry.get(kind)


def instrument_resolves(kind: Optional[str], licence_type: Optional[str]) -> bool:
    """Whether the gating instrument for a document is known for this licence."""
    if kind is None:
        return True
    try:
        _resolve_instrument(kind, licence_type)
        return True
    except (ValueError, KeyError):
        return False


def expected_doc_types(licence_type: Optional[str]) -> Tuple[str, ...]:
    """The doc_types a complete pack must contain for this licence.

    The completeness gate and the generator must agree on this, so both call it
    rather than each keeping a list. Resolved per document against the registry:
    an unknown licence yields only the IT Audit & VAPT log, an MPI additionally
    yields the two Cyber Hygiene Notice documents, a bank yields all seven.
    """
    return tuple(
        s.doc_type for s in TRM_DOCUMENT_CATALOG
        if instrument_resolves(s.requires_instrument, licence_type)
    )


def _word_count(content: Any) -> int:
    if isinstance(content, str):
        return len(content.split())
    return len(json.dumps(content, ensure_ascii=False).split())


def generate_all_trm_documents(
    ctx: TrmDocContext,
    only_doc_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate the pack. Returns one result dict per attempted document.

    A document whose binding instrument is not mapped for this licence class is
    SKIPPED (not failed, not defaulted) — the caller sets the pack status to
    blocked_licence_unknown and asks the customer to confirm. Which documents
    those are depends on the licence, not just on whether one is set: see
    expected_doc_types.

    `only_doc_types` regenerates a subset — the licence-correction path, which
    re-runs whatever became resolvable once the customer confirms.
    """
    results: List[Dict[str, Any]] = []
    for spec in TRM_DOCUMENT_CATALOG:
        if only_doc_types is not None and spec.doc_type not in only_doc_types:
            continue
        if not instrument_resolves(spec.requires_instrument, ctx.licence_type):
            logger.info(
                "[TRMDoc] skipping %s — no %s mapped for licence %r",
                spec.doc_type, spec.requires_instrument, ctx.licence_type,
            )
            results.append({
                "doc_type": spec.doc_type, "title": spec.title,
                "mode": spec.mode, "content": None,
                "skipped": True, "skip_reason": "mas_licence_type_not_confirmed",
                "schema_version": TRM_DOC_SCHEMA_VERSION,
            })
            continue

        logger.info("[TRMDoc] generating %s via DeepSeek", spec.doc_type)
        try:
            content, in_tok, out_tok = _call_with_retry(spec.fn, ctx)
            cost = (in_tok / 1_000_000 * _IN) + (out_tok / 1_000_000 * _OUT)
            # The sentinels resolve to this entity's actual instrument
            # (FSM-N05/N03/..., FSM-N06/N14/...) rather than a fixed registry
            # key. See the TRM_DOCUMENT_CATALOG comment.
            resolved_notices = []
            title = spec.title
            for k in spec.notices:
                inst = _resolve_instrument(k, ctx.licence_type)
                resolved_notices.append(inst.citation)
                if k == _CYBER_HYGIENE_NOTICE:
                    title = f"{spec.title} (MAS Notice {inst.ids[0]})"
            row: Dict[str, Any] = {
                "doc_type": spec.doc_type,
                "title": title,
                "mode": spec.mode,
                "content": content,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": round(cost, 5),
                "word_count": _word_count(content),
                "generated_by_model": DEEPSEEK_MODEL,
                "schema_version": TRM_DOC_SCHEMA_VERSION,
                "verification_critical": spec.verification_critical,
                "notices": resolved_notices,
                "domain": spec.domain,
            }
            if spec.doc_type == "outsourcing_risk_register":
                row["outsourcing_regime"] = outsourcing_regime(ctx.licence_type)
                row["notices"] = [
                    i.citation for i in registry.outsourcing_instruments(ctx.licence_type)
                ]
            results.append(row)
            logger.info("[TRMDoc] ✓ %s — %d words | $%.5f",
                        spec.doc_type, row["word_count"], cost)
        except Exception as e:  # noqa: BLE001 — recorded per doc, gate decides
            logger.error("[TRMDoc] ✗ %s failed: %s", spec.doc_type, e, exc_info=True)
            results.append({
                "doc_type": spec.doc_type, "title": spec.title,
                "mode": spec.mode, "content": None, "error": str(e),
                "schema_version": TRM_DOC_SCHEMA_VERSION,
            })

    total = sum(r.get("cost_usd", 0) for r in results)
    ok = sum(1 for r in results if r.get("content") is not None)
    logger.info("[TRMDoc] pack complete — %d/%d generated | $%.4f",
                ok, len(results), total)
    return results
