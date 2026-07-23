"""Inspection-ready record export for nominee fit-and-proper and STR decisions.

Both records already existed as database rows with a SHA-256 hash anchored
on-chain — but there was nothing an ACRA inspector could actually be handed. A
hash with no rendered record is not inspection evidence: if an inspector asks
"why didn't you file an STR on this client", the answer has to be a document
carrying the CSP's own reasoning, not a transaction ID.

The reasoning in these records is written by the CSP, not generated. Under the
CSP Act 2024 the fit-and-proper assessment must be performed by the registered
CSP itself, and an STR decision — including a decision *not* to file — needs a
defensible rationale on file. This module renders what the CSP wrote, verbatim,
alongside the checks performed and the blockchain anchor. It does not author it,
and it says so in print.

Body text is markdown-ish and handed to
`csp_doc_generator.generate_csp_document_pdf`, which already handles heading and
bullet parsing, XML-escaping, the legal disclaimer page, and hashing. No new
ReportLab layout code belongs here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Bump when the visible structure of an exported record changes.
CSP_RECORD_SCHEMA_VERSION = 1

_PROVENANCE = (
    "This record is the assessment made by the corporate service provider named "
    "above. Booppa records, renders, and notarizes it — Booppa did not author the "
    "reasoning it contains and does not attest to its adequacy."
)


def _fmt_dt(value: Any) -> str:
    """Render a datetime as a date, or an explicit dash when absent."""
    if isinstance(value, datetime):
        return value.strftime("%d %B %Y")
    return str(value) if value else "—"


def _check_line(label: str, done: Any, statute: str) -> str:
    """Render a statutory check as performed/NOT performed — never a silent blank.

    A blank cell reads as "no answer"; an inspector reads it as "not done". Say
    which it is.
    """
    mark = "Performed" if done else "NOT performed"
    return f"- **{label}:** {mark} — {statute}"


def _anchor_section(evidence: Optional[Any], fallback_tx: Any = None,
                    fallback_url: Any = None) -> str:
    """Blockchain anchor block, or an honest "pending" when nothing is anchored yet.

    `evidence` is a CspBlockchainEvidence row (see GET /csp/evidence); the
    fallbacks are the tx fields denormalised onto the record itself, which the
    notarization task writes back.
    """
    tx = getattr(evidence, "tx_hash", None) or fallback_tx
    url = getattr(evidence, "polygonscan_url", None) or fallback_url
    if not tx:
        return (
            "## Blockchain Anchor\n"
            "\n"
            "Not yet anchored. Notarization runs asynchronously shortly after the "
            "record is saved; re-export this record once it completes to obtain a "
            "verifiable anchor.\n"
        )
    lines = [
        "## Blockchain Anchor",
        "",
        "The SHA-256 hash of this record is anchored on-chain, so the version you "
        "hold can be shown to be the version recorded on the decision date.",
        "",
        f"- **Transaction hash:** {tx}",
    ]
    doc_hash = getattr(evidence, "document_hash", None)
    if doc_hash:
        lines.append(f"- **Record hash (SHA-256):** {doc_hash}")
    network = getattr(evidence, "network", None)
    if network:
        lines.append(f"- **Network:** {network}")
    block = getattr(evidence, "block_number", None)
    if block:
        lines.append(f"- **Block:** {block}")
    anchored_at = getattr(evidence, "blockchain_timestamp", None)
    if anchored_at:
        lines.append(f"- **Anchored:** {_fmt_dt(anchored_at)}")
    if url:
        lines.append(f"- **Verify:** {url}")
    return "\n".join(lines) + "\n"


def _header(profile, subtitle: str) -> str:
    return (
        f"## Assessed Entity\n"
        f"\n"
        f"- **Corporate service provider:** {profile.legal_name}\n"
        f"- **UEN:** {profile.uen}\n"
        f"- **Record type:** {subtitle}\n"
        f"\n"
        f"{_PROVENANCE}\n"
    )


def build_nominee_assessment_record(
    nominee, profile, evidence=None, client=None,
) -> Tuple[str, str]:
    """Render a nominee director fit-and-proper assessment record.

    Returns ``(title, body)`` for `generate_csp_document_pdf`.
    """
    title = f"Nominee Director Fit-and-Proper Assessment — {nominee.nominee_full_name}"

    status = getattr(nominee.assessment_status, "value", nominee.assessment_status)
    outcome_label = {
        "fit_proper":   "FIT AND PROPER",
        "not_fit":      "NOT FIT AND PROPER",
        "under_review": "UNDER REVIEW",
        "not_assessed": "NOT ASSESSED",
    }.get(str(status), str(status).upper())

    parts = [
        _header(profile, "Fit-and-proper assessment under the Corporate Service "
                         "Providers Act 2024"),
        "## 1. Subject of Assessment",
        "",
        f"- **Nominee director:** {nominee.nominee_full_name}",
        f"- **Nationality:** {nominee.nominee_nationality or '—'}",
        f"- **Nominator:** {nominee.nominator_name}",
        f"- **Company:** {nominee.company_name or '—'}"
        + (f" (UEN {nominee.company_uen})" if nominee.company_uen else ""),
        f"- **Client of record:** {getattr(client, 'legal_name', None) or '—'}",
        f"- **Appointment date:** {_fmt_dt(nominee.appointment_date)}",
        f"- **Arrangement active:** {'Yes' if nominee.is_active else 'No'}"
        + (f" (ceased {_fmt_dt(nominee.cessation_date)})" if nominee.cessation_date else ""),
        "",
        "## 2. Checks Performed",
        "",
        "Under the CSP Act 2024 the registered corporate service provider must "
        "itself perform these checks. Each is recorded below as performed or not "
        "performed — there is no implied answer.",
        "",
        _check_line("Criminal record check", nominee.criminal_check_done,
                    "convictions bearing on honesty, fraud, or financial impropriety"),
        _check_line("Bankruptcy check", nominee.bankruptcy_check_done,
                    "an undischarged bankrupt may not act as a director"),
        _check_line("Director history check", nominee.director_history_check,
                    "prior disqualifications and struck-off or wound-up entities"),
        "",
        "## 3. Outcome",
        "",
        f"- **Determination:** {outcome_label}",
        f"- **Assessed by:** {nominee.assessed_by or '—'}",
        f"- **Assessment date:** {_fmt_dt(nominee.assessment_date)}",
        f"- **Next review due:** {_fmt_dt(nominee.next_review)}",
        "",
        "### Stated outcome",
        "",
        nominee.assessment_outcome or "Not recorded.",
        "",
        "### Assessor's reasoning",
        "",
        "The text below is reproduced verbatim as recorded by the corporate "
        "service provider. It is the answer to an inspector asking why this "
        "person was passed or failed.",
        "",
        nominee.assessment_notes or "Not recorded.",
        "",
        "## 4. ACRA Disclosure",
        "",
        "From 16 June 2025 all nominee directors must be filed with ACRA. Nominee "
        "status is made public; the nominator's identity is not.",
        "",
        f"- **Disclosed to ACRA:** {'Yes' if nominee.acra_disclosed else 'No'}",
        f"- **Filing date:** {_fmt_dt(nominee.acra_filing_date)}",
        f"- **Filing reference:** {nominee.acra_filing_ref or '—'}",
        "",
        _anchor_section(evidence, nominee.blockchain_tx_hash, nominee.polygonscan_url),
        "",
        f"Record schema v{CSP_RECORD_SCHEMA_VERSION}.",
    ]
    return title, "\n".join(parts)


def build_str_decision_record(
    report, profile, client=None, evidence=None,
) -> Tuple[str, str]:
    """Render an STR decision record — including a decision *not* to file.

    Returns ``(title, body)`` for `generate_csp_document_pdf`.
    """
    decision = str(getattr(report.decision, "value", report.decision))
    decision_label = {
        "filed":     "STR FILED with the Suspicious Transaction Reporting Office",
        "not_filed": "STR NOT FILED",
        "pending":   "DECISION PENDING",
        "escalated": "ESCALATED TO SENIOR MANAGEMENT",
    }.get(decision, decision.upper())

    title = f"Suspicious Transaction Report Decision Record — {decision.replace('_', ' ').title()}"

    parts = [
        _header(profile, "STR decision record under the Corporate Service "
                         "Providers Act 2024 and the CDSA"),
        "## 1. Trigger",
        "",
        f"- **Client:** {getattr(client, 'legal_name', None) or '—'}",
        f"- **Trigger type:** {report.trigger_type or '—'}",
        f"- **Amount involved:** "
        + (f"{report.currency or 'SGD'} {report.amount_involved:,.2f}"
           if report.amount_involved is not None else "—"),
        f"- **Transaction date:** {_fmt_dt(report.transaction_date)}",
        "",
        "### What was observed",
        "",
        report.trigger_detail or "Not recorded.",
        "",
        "## 2. Decision",
        "",
        f"- **Decision:** {decision_label}",
        f"- **Decided by:** {report.decision_by or '—'}",
        f"- **Decision date:** {_fmt_dt(report.decision_date)}",
        f"- **Service declined:** {'Yes' if report.service_declined else 'No'}",
    ]

    if decision == "filed":
        parts += [
            f"- **STRO reference:** {report.stro_reference or '—'}",
            f"- **Filed on:** {_fmt_dt(report.stro_filed_date)}",
            f"- **Filed by:** {report.stro_filed_by or '—'}",
        ]
    if report.escalated_to_senior_mgmt:
        parts += [
            f"- **Escalated to:** {report.senior_mgmt_name or '—'}",
            f"- **Escalation date:** {_fmt_dt(report.escalation_date)}",
        ]

    parts += ["", "### Rationale", ""]
    if decision == "not_filed":
        parts += [
            "A decision not to file is itself a decision, and it must be logged "
            "with its rationale. The text below is what answers an ACRA inspector "
            "asking why no report was made on this client.",
            "",
        ]
    else:
        parts += [
            "Reproduced verbatim as recorded by the corporate service provider on "
            "the decision date.",
            "",
        ]
    parts += [
        report.decision_rationale or "Not recorded.",
        "",
        "## 3. Tipping-Off",
        "",
        f"- **Client notified:** {'Yes' if report.client_notified else 'No'}",
        "",
        "Client notification is permanently disabled in this system. Tipping-off "
        "is an offence under section 48A of the Corruption, Drug Trafficking and "
        "Other Serious Crimes (Confiscation of Benefits) Act — a fine of up to "
        "S$250,000 and/or imprisonment of up to 3 years.",
        "",
        _anchor_section(evidence, report.blockchain_tx_hash, report.polygonscan_url),
        "",
        f"Record schema v{CSP_RECORD_SCHEMA_VERSION}.",
    ]
    return title, "\n".join(parts)
