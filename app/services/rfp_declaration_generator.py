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
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, KeepTogether, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Logo resolution mirrors cover_sheet_generator.py.
_HERE = os.path.dirname(__file__)
_LOGO_CANDIDATES = [
    os.path.join(_HERE, "..", "..", "static", "logo.png"),
    "/app/static/logo.png",
    os.path.join(_HERE, "..", "..", "data", "logo.png"),
    "/app/data/logo.png",
]
_LOGO_PATH: str | None = None
for _c in _LOGO_CANDIDATES:
    _abs = os.path.abspath(_c)
    if os.path.exists(_abs):
        _LOGO_PATH = _abs
        break

PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
HEADER_H = 0.7 * inch
FOOTER_H = 0.45 * inch

NAVY    = colors.HexColor("#0f172a")
EMERALD = colors.HexColor("#10b981")
SLATE   = colors.HexColor("#64748b")
LIGHT   = colors.HexColor("#f8fafc")
BORDER  = colors.HexColor("#e2e8f0")
WHITE   = colors.white
BLUE    = colors.HexColor("#0284c7")
AMBER   = colors.HexColor("#d97706")

_STYLES: Dict[str, ParagraphStyle] = {
    "normal": ParagraphStyle("decl_normal", fontSize=8, leading=12, textColor=colors.HexColor("#334155")),
    "h1":     ParagraphStyle("decl_h1", fontSize=16, leading=20, textColor=NAVY, fontName="Helvetica-Bold"),
    "h2":     ParagraphStyle("decl_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
    "small":  ParagraphStyle("decl_small", fontSize=7, leading=10, textColor=SLATE),
    "item_t": ParagraphStyle("decl_item_t", fontSize=9, leading=12, textColor=NAVY, fontName="Helvetica-Bold"),
    "item_b": ParagraphStyle("decl_item_b", fontSize=8, leading=11, textColor=colors.HexColor("#334155")),
    "badge":  ParagraphStyle("decl_badge", fontSize=6, leading=8, textColor=WHITE, fontName="Helvetica-Bold", alignment=1),
}


def _xml_escape(s: Any) -> str:
    """Escape user-supplied text so ReportLab's Paragraph mini-XML doesn't
    misinterpret `&`, `<`, `>`."""
    return (
        (str(s) if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _draw_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    logo_h = 0.48 * inch
    logo_w = logo_h * 2.94
    logo_y = PAGE_H - HEADER_H + (HEADER_H - logo_h) / 2
    logo_drawn = False
    if _LOGO_PATH:
        try:
            canvas.drawImage(
                _LOGO_PATH, MARGIN, logo_y,
                width=logo_w, height=logo_h,
                preserveAspectRatio=True, mask="auto",
            )
            logo_drawn = True
        except Exception:
            logo_drawn = False
    if not logo_drawn:
        canvas.setFillColor(EMERALD)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(MARGIN, PAGE_H - HEADER_H + 0.26 * inch, "BOOPPA")

    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(
        PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.26 * inch,
        "SUPPLIER COMPLIANCE DECLARATION",
    )

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, "Booppa Smart Care LLC · booppa.io · Confidential")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


def _badge(text: str, color) -> Paragraph:
    style = ParagraphStyle(
        "b_" + text.replace(" ", "_"), parent=_STYLES["badge"], backColor=color, borderPadding=2,
    )
    return Paragraph(text, style)


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{_xml_escape(k)}</b>", _STYLES["normal"]),
             Paragraph(_xml_escape(v), _STYLES["normal"])] for k, v in rows]
    t = Table(data, colWidths=[2.2 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT, WHITE]),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _declaration_item(code: str, title: str, body: str, badge_text: str, badge_color):
    """One D-item card: code + title + status badge, declaration body underneath."""
    header = Table(
        [[
            Paragraph(f"{_xml_escape(code)}. {_xml_escape(title)}", _STYLES["item_t"]),
            _badge(badge_text, badge_color),
        ]],
        colWidths=[5.1 * inch, 1.7 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (1, 0), (1, 0), badge_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    body_t = Table([[Paragraph(body, _STYLES["item_b"])]], colWidths=[6.8 * inch])
    body_t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBEFORE", (0, 0), (0, -1), 2, badge_color),
    ]))
    return KeepTogether([header, body_t, Spacer(1, 0.12 * inch)])


# Badge presentation.
_VERIFIED = ("VERIFIED — BOOPPA", EMERALD)
_DECLARED = ("CLIENT-DECLARED", AMBER)


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

        company = company_name or vendor_ctx.get("acra_name") or "Your Organisation"
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
        story.append(_declaration_item(
            "D1", "Conflict of Interest",
            f"{_xml_escape(company)} declares that, to the best of its knowledge, no "
            "director, officer, or employee involved in this submission has any actual, "
            "potential, or perceived conflict of interest with the procuring agency or "
            "the evaluation of this tender. Any such interest arising during the tender "
            "will be disclosed in writing without delay.",
            *_DECLARED,
        ))

        # ── D2 Non-collusion ───────────────────────────────────────────────
        story.append(_declaration_item(
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
        story.append(_declaration_item(
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
        story.append(_declaration_item("D4", "PDPA Compliance", d4_body, *d4_badge))

        # ── D5 Confidentiality undertaking ─────────────────────────────────
        story.append(_declaration_item(
            "D5", "Confidentiality Undertaking",
            "The supplier undertakes to keep confidential all information disclosed by the "
            "procuring agency in connection with this tender, to use it solely for the "
            "purpose of this submission, and not to disclose it to any third party without "
            "prior written consent.",
            *_DECLARED,
        ))

        # ── D6 Accuracy of information + signature ──────────────────────────
        story.append(_declaration_item(
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
        doc = BaseDocTemplate(
            buf, pagesize=A4,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=HEADER_H + 0.2 * inch, bottomMargin=FOOTER_H + 0.2 * inch,
        )
        frame = Frame(
            MARGIN, FOOTER_H + 0.1 * inch,
            PAGE_W - 2 * MARGIN, PAGE_H - HEADER_H - FOOTER_H - 0.3 * inch,
            id="body",
        )
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_page)])
        doc.build(story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Supplier declaration PDF generation failed: {e}")
        return b""
