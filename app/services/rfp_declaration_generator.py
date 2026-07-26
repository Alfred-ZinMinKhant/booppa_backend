"""
RFP Supplier Compliance Declaration generator (Sprint 5c)
=========================================================
Third RFP-kit output, alongside the evidence PDF and editable DOCX.

There is NO single, public, standardised "GeBIZ Appendix D" form — appendices
are numbered per-tender, so "Appendix D" means different things in different
solicitations. This generator instead produces a neutral **Supplier Compliance
Declaration** covering the declarations that recur across virtually all
Singapore government tenders (MOF procurement regime): conflict of interest,
non-collusion, debarment/blacklisting status, confidentiality, PDPA compliance,
and accuracy of submitted information.

Each item is tagged **VERIFIED** (corroborated by Booppa data — ACRA / PDPA
score) or **CLIENT-DECLARED** (the authorised signatory attests to it). A
disclaimer tells the buyer to map the declaration to their specific tender's
appendix.

Honesty rule: Booppa cannot query the MOF debarment register, so D3 is always
CLIENT-DECLARED, with ACRA entity status shown only as corroboration — never a
bare "VERIFIED".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, Spacer, Table

from app.services import pdf_layout as _pl
from app.services import rfp_doc_common as _common
from app.services.pdf_layout import (
    BORDER, CONTENT_W, EMERALD, LIGHT, NAVY, SLATE, WHITE, xml_escape as _xml_escape,
)
from app.services.rfp_doc_common import AMBER, BLUE

logger = logging.getLogger(__name__)

# Logo, geometry, palette and page furniture come from pdf_layout; the
# styles/badge/item-card that this document shares with its sibling live in
# rfp_doc_common.
_STYLES = _common.make_styles("decl_")
_HEADER_LABEL = "SUPPLIER COMPLIANCE DECLARATION"
_FOOTER_TEXT = (
    "Booppa Smart Care LLC · booppa.io · Confidential"
)


def _kv_table(rows) -> Table:
    return _common.kv_table(rows, _STYLES)


def _declaration_item(code, title, body, badge_text, badge_color) -> list:
    """One D-item card. Returns a list of flowables — the previous
    KeepTogether-around-a-one-row-Table was doubly unbreakable and clipped
    long answers off a document that gets signed and anchored."""
    return _common.item_card(
        code, title, body, badge_text, badge_color, _STYLES,
        separator=". ",
    )


_VERIFIED = _common.VERIFIED
_DECLARED = _common.DECLARED


def build_supplier_declaration_pdf(
    company_name: str,
    vendor_ctx: Optional[Dict[str, Any]] = None,
    intake: Optional[Dict[str, Any]] = None,
    verification_map: Optional[Dict[str, Any]] = None,
    acra_live: Optional[Dict[str, Any]] = None,
    pdpc_result: Optional[Dict[str, Any]] = None,
    compliance_score: Optional[int] = None,
    tx_hash: Optional[str] = None,
    report_id: Optional[str] = None,
) -> bytes:
    """Render the Supplier Compliance Declaration PDF. Returns PDF bytes
    (empty bytes on failure — caller treats this as a non-blocking warning)."""
    try:
        vendor_ctx = vendor_ctx or {}
        intake = intake or {}
        acra_live = acra_live or {}
        pdpc_result = pdpc_result or {}

        # Intake-first: the report-scoped name resolved by the builder outranks the
        # `company_name` argument, which may be checkout/account-derived (SPQR leak).
        company = (
            intake.get("company_name")
            or vendor_ctx.get("company_name")
            or company_name
            or vendor_ctx.get("acra_name")
            or "Your Organisation"
        )
        uen = vendor_ctx.get("uen") or intake.get("uen") or "_______________"
        acra_name = vendor_ctx.get("acra_name")
        entity_status = acra_live.get("entity_status") or acra_live.get("status")
        acra_found = bool(acra_live.get("found"))

        if compliance_score is None:
            compliance_score = (
                intake.get("compliance_score")
                or pdpc_result.get("compliance_score")
            )

        story: list = []

        # ── Title + intro ──────────────────────────────────────────────────
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("Supplier Compliance Declaration", _STYLES["h1"]))
        story.append(Spacer(1, 0.04 * inch))
        story.append(Paragraph(
            "For attachment to Singapore Government (GeBIZ) tender submissions. "
            "This declaration consolidates the supplier representations that recur "
            "across government solicitations. Map each item to the corresponding "
            "appendix or declaration of your specific tender — appendix lettering "
            "(e.g. \"Appendix D\") is assigned per-tender and is not standardised "
            "across solicitations.",
            _STYLES["small"],
        ))
        story.append(Spacer(1, 0.12 * inch))

        # ── Supplier particulars ───────────────────────────────────────────
        rows = [("Supplier", company)]
        if acra_name and acra_name != company:
            rows.append(("ACRA Registered Name", acra_name))
        rows.append(("UEN (Business Reg. No.)", uen))
        if entity_status:
            rows.append(("ACRA Entity Status", entity_status))
        rows.append(("Declaration Date", datetime.now(timezone.utc).strftime("%d %b %Y")))
        if report_id:
            rows.append(("Reference ID", report_id))
        if tx_hash:
            rows.append(("Blockchain Anchor", tx_hash))
        story.append(_kv_table(rows))
        story.append(Spacer(1, 0.16 * inch))

        story.append(Paragraph("Declarations", _STYLES["h2"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))

        # ── D1 Conflict of interest ────────────────────────────────────────
        story.extend(_declaration_item(
            "D1", "Conflict of Interest",
            f"{_xml_escape(company)} declares that, to the best of its knowledge, no "
            "director, officer, or employee involved in this submission has any actual, "
            "potential, or perceived conflict of interest with the procuring agency or "
            "the evaluation of this tender. Any such interest arising during the tender "
            "will be disclosed in writing without delay.",
            *_DECLARED,
        ))

        # ── D2 Non-collusion ───────────────────────────────────────────────
        story.extend(_declaration_item(
            "D2", "Non-Collusion",
            "The supplier declares that this offer is made independently and without "
            "any agreement, communication, or arrangement with any competitor to fix "
            "prices, restrict competition, or otherwise collude in respect of this "
            "tender.",
            *_DECLARED,
        ))

        # ── D3 Debarment / blacklisting (declared + ACRA corroboration) ─────
        if acra_found and entity_status:
            corroboration = (
                f" ACRA records retrieved by Booppa show this entity's status as "
                f"\"{_xml_escape(entity_status)}\" as corroborating context — note this "
                "is an ACRA registration check, not a check of the Government's "
                "debarment register, which only the procuring agency can access."
            )
        else:
            corroboration = (
                " Booppa was unable to corroborate this declaration against ACRA "
                "records; it rests on the supplier's attestation alone."
            )
        story.extend(_declaration_item(
            "D3", "Debarment / Blacklisting Status",
            "The supplier declares that it is not currently debarred, suspended, or "
            "blacklisted from participating in Singapore Government procurement, and is "
            "not the subject of any proceedings that could lead to such status."
            + corroboration,
            *_DECLARED,
        ))

        # ── D4 PDPA compliance (verified by Booppa scan) ───────────────────
        if compliance_score is not None:
            d4_body = (
                "The supplier maintains a Personal Data Protection Act (PDPA) compliance "
                "programme. Booppa's automated PDPA assessment of the supplier returned a "
                f"compliance score of {int(compliance_score)}/100, anchored as evidence in "
                "this kit. Full findings accompany the RFP evidence certificate."
            )
            d4_badge = _VERIFIED
        else:
            d4_body = (
                "The supplier declares that it complies with the Personal Data Protection "
                "Act (PDPA) and has appropriate policies and safeguards in place. A Booppa "
                "PDPA assessment score was not available at generation time."
            )
            d4_badge = _DECLARED
        story.extend(_declaration_item("D4", "PDPA Compliance", d4_body, *d4_badge))

        # ── D5 Confidentiality undertaking ─────────────────────────────────
        story.extend(_declaration_item(
            "D5", "Confidentiality Undertaking",
            "The supplier undertakes to keep confidential all information disclosed by the "
            "procuring agency in connection with this tender, to use it solely for the "
            "purpose of this submission, and not to disclose it to any third party without "
            "prior written consent.",
            *_DECLARED,
        ))

        # ── D6 Accuracy of information + signature ──────────────────────────
        story.extend(_declaration_item(
            "D6", "Accuracy of Information",
            "The authorised signatory declares that all information provided in this "
            "declaration and the accompanying RFP evidence kit is true, accurate, and "
            "complete to the best of their knowledge as at the declaration date above.",
            *_DECLARED,
        ))

        # ── Signature block ────────────────────────────────────────────────
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("Authorised Signatory", _STYLES["h2"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))
        sig_rows = [
            ("Signature", "____________________________________"),
            ("Name", "____________________________________"),
            ("Designation", "____________________________________"),
            ("Date", "____________________________________"),
            ("Company Stamp", "____________________________________"),
        ]
        story.append(_kv_table(sig_rows))

        story.append(Spacer(1, 0.14 * inch))
        story.append(Paragraph(
            "Legend: <b>VERIFIED — BOOPPA</b> items are corroborated by Booppa data "
            "(ACRA records / automated PDPA assessment). <b>CLIENT-DECLARED</b> items "
            "rest on the authorised signatory's attestation. This document is a "
            "supplier-prepared declaration and does not constitute legal advice or a "
            "government certification.",
            _STYLES["small"],
        ))

        # ── Render ─────────────────────────────────────────────────────────
        buf = BytesIO()
        doc = _common.build_doc(
            buf, title="Supplier Compliance Declaration",
            header_label=_HEADER_LABEL, footer_text=_FOOTER_TEXT,
        )
        _pl.render(doc, story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Supplier declaration PDF generation failed: {e}")
        return b""
