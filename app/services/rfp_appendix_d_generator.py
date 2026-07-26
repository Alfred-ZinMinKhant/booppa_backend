"""
RFP "Appendix D" data-protection appendix generator (best-effort generic)
=========================================================================
Fourth RFP Complete-kit output, alongside the evidence PDF, editable DOCX, and
the Supplier Compliance Declaration.

There is NO single, public, standardised "GeBIZ Appendix D" form — appendices
are lettered/numbered **per-tender**, so "Appendix D" means different things in
different solicitations. This generator therefore produces a *generic template*:
the supplier's actual data-protection & cybersecurity answers (the kit's Q&A),
laid out as a numbered D.1 … D.n appendix that a bidder can paste into — and
renumber to match — the data-protection appendix of their specific ITT.

A persistent banner on every page makes the "generic template — verify the
numbering against your tender" caveat impossible to miss, so the output is never
mistaken for an official, tender-specific appendix.

Each item is tagged **VERIFIED — BOOPPA** (corroborated by Booppa data: ACRA /
PDPA score / website signals) or **CLIENT-DECLARED** (the supplier's own
answer), reusing the same verification signal as the Supplier Declaration.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, Spacer, Table, TableStyle

from app.services import pdf_layout as _pl
from app.services import rfp_doc_common as _common
from app.services.pdf_layout import (
    BORDER, CONTENT_W, EMERALD, LIGHT, NAVY, SLATE, WHITE, xml_escape as _xml_escape,
)
from app.services.rfp_doc_common import AMBER, AMBER_BG, BLUE

logger = logging.getLogger(__name__)

# Logo, geometry, palette and page furniture come from pdf_layout; the
# styles/badge/item-card that this document shares with its sibling live in
# rfp_doc_common.
_STYLES = _common.make_styles("apx_")
_HEADER_LABEL = "DATA PROTECTION APPENDIX — GENERIC TEMPLATE"
_FOOTER_TEXT = (
    "Booppa Smart Care LLC · booppa.io · Generic template — verify appendix numbering against your tender"
)


def _kv_table(rows) -> Table:
    return _common.kv_table(rows, _STYLES)


def _appendix_item(code, title, body, badge_text, badge_color, evidence=None) -> list:
    """One D-item card. Returns a list of flowables — the previous
    KeepTogether-around-a-one-row-Table was doubly unbreakable and clipped
    long answers off a document that gets signed and anchored."""
    return _common.item_card(
        code, title, body, badge_text, badge_color, _STYLES,
        separator="  ", evidence=evidence,
    )


_VERIFIED = _common.VERIFIED
_DECLARED = _common.DECLARED


def build_appendix_d_pdf(
    company_name: str,
    qa_items: Optional[List[Dict[str, Any]]] = None,
    vendor_ctx: Optional[Dict[str, Any]] = None,
    intake: Optional[Dict[str, Any]] = None,
    acra_live: Optional[Dict[str, Any]] = None,
    compliance_score: Optional[int] = None,
    tx_hash: Optional[str] = None,
    report_id: Optional[str] = None,
    coverage_summary: Optional[str] = None,
) -> bytes:
    """Render the generic "Appendix D" data-protection template PDF.

    ``qa_items`` is the kit's labelled Q&A: a list of
    ``{"question": str, "answer": str, "verified": bool}`` dicts (the same
    data behind the evidence PDF / result page). Each becomes a numbered D-item.

    Returns PDF bytes (empty bytes on failure — caller treats this as a
    non-blocking warning, exactly like the Supplier Declaration).
    """
    try:
        qa_items = qa_items or []
        vendor_ctx = vendor_ctx or {}
        intake = intake or {}
        acra_live = acra_live or {}

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

        if compliance_score is None:
            compliance_score = intake.get("compliance_score") or vendor_ctx.get("compliance_score")

        story: list = []

        # ── Title ──────────────────────────────────────────────────────────
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("Data Protection &amp; Cybersecurity Appendix", _STYLES["h1"]))
        story.append(Spacer(1, 0.06 * inch))

        # ── Prominent "generic template" banner ────────────────────────────
        banner_txt = (
            "GENERIC TEMPLATE — NOT TENDER-SPECIFIC. Singapore (GeBIZ) tender "
            "appendices are lettered and numbered per-tender; there is no fixed, "
            "universal \"Appendix D\". The items below (D.1, D.2 …) reproduce your "
            "data-protection responses in a reusable layout. Before submission, map "
            "each item to — and renumber it to match — the data-protection appendix "
            "of your specific Invitation to Tender (ITT)."
        )
        banner = Table([[Paragraph(banner_txt, _STYLES["warn"])]], colWidths=[6.8 * inch])
        banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG),
            ("BOX", (0, 0), (-1, -1), 0.8, AMBER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(banner)
        story.append(Spacer(1, 0.14 * inch))

        # ── Supplier particulars ───────────────────────────────────────────
        rows = [("Supplier", company)]
        if acra_name and acra_name != company:
            rows.append(("ACRA Registered Name", acra_name))
        rows.append(("UEN (Business Reg. No.)", uen))
        if entity_status:
            rows.append(("ACRA Entity Status", entity_status))
        if compliance_score is not None:
            rows.append(("Booppa PDPA Compliance Score", f"{int(compliance_score)}/100"))
        rows.append(("Prepared", datetime.now(timezone.utc).strftime("%d %b %Y")))
        if report_id:
            rows.append(("Reference ID", report_id))
        if tx_hash:
            rows.append(("Blockchain Anchor", tx_hash))
        if coverage_summary:
            rows.append(("Coverage", coverage_summary))
        story.append(_kv_table(rows))
        story.append(Spacer(1, 0.16 * inch))

        story.append(Paragraph("Appendix D — Data Protection &amp; Cybersecurity Responses", _STYLES["h2"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))

        # ── D.n items from the kit Q&A ─────────────────────────────────────
        if qa_items:
            for i, item in enumerate(qa_items, start=1):
                question = (item.get("question") or item.get("label") or f"Item {i}").strip()
                answer = (item.get("answer") or "").strip() or "—"
                verified = bool(item.get("verified"))
                badge = _VERIFIED if verified else _DECLARED
                story.extend(_appendix_item(
                    f"D.{i}",
                    question,
                    _xml_escape(answer),
                    badge[0],
                    badge[1],
                    evidence=item.get("evidence"),
                ))
        else:
            story.append(Paragraph(
                "No data-protection responses were available at generation time. "
                "Complete your RFP intake to populate this appendix.",
                _STYLES["normal"],
            ))

        # ── Legend + disclaimer ─────────────────────────────────────────────
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            "Legend: <b>VERIFIED — BOOPPA</b> responses are corroborated by Booppa "
            "data (ACRA records, automated PDPA assessment, or website signals). "
            "<b>CLIENT-DECLARED</b> responses are the supplier's own statements. This "
            "appendix is a supplier-prepared template, not a government certification "
            "or legal advice; confirm wording and numbering against your specific tender.",
            _STYLES["small"],
        ))

        # ── Render ─────────────────────────────────────────────────────────
        buf = BytesIO()
        doc = _common.build_doc(
            buf, title="Data Protection Appendix",
            header_label=_HEADER_LABEL, footer_text=_FOOTER_TEXT,
        )
        _pl.render(doc, story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Appendix D PDF generation failed: {e}")
        return b""
