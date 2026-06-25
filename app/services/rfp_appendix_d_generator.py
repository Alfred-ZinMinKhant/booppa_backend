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
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, KeepTogether, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Logo resolution mirrors rfp_declaration_generator.py / cover_sheet_generator.py.
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
AMBER   = colors.HexColor("#d97706")
AMBER_BG = colors.HexColor("#fffbeb")

_STYLES: Dict[str, ParagraphStyle] = {
    "normal": ParagraphStyle("apx_normal", fontSize=8, leading=12, textColor=colors.HexColor("#334155")),
    "h1":     ParagraphStyle("apx_h1", fontSize=16, leading=20, textColor=NAVY, fontName="Helvetica-Bold"),
    "h2":     ParagraphStyle("apx_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
    "small":  ParagraphStyle("apx_small", fontSize=7, leading=10, textColor=SLATE),
    "warn":   ParagraphStyle("apx_warn", fontSize=7.5, leading=11, textColor=AMBER, fontName="Helvetica-Bold"),
    "item_t": ParagraphStyle("apx_item_t", fontSize=9, leading=12, textColor=NAVY, fontName="Helvetica-Bold"),
    "item_b": ParagraphStyle("apx_item_b", fontSize=8, leading=11, textColor=colors.HexColor("#334155")),
    "badge":  ParagraphStyle("apx_badge", fontSize=6, leading=8, textColor=WHITE, fontName="Helvetica-Bold", alignment=1),
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
        "DATA PROTECTION APPENDIX — GENERIC TEMPLATE",
    )

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, "Booppa Smart Care LLC · booppa.io · Generic template — verify appendix numbering against your tender")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


def _badge(text: str, color) -> Paragraph:
    style = ParagraphStyle(
        "ab_" + text.replace(" ", "_"), parent=_STYLES["badge"], backColor=color, borderPadding=2,
    )
    return Paragraph(text, style)


def _kv_table(rows: List[tuple]) -> Table:
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


def _appendix_item(code: str, title: str, body: str, badge_text: str, badge_color):
    """One D-item card: code + question, supplier answer underneath, status badge."""
    header = Table(
        [[
            Paragraph(f"{_xml_escape(code)}  {_xml_escape(title)}", _STYLES["item_t"]),
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


_VERIFIED = ("VERIFIED — BOOPPA", EMERALD)
_DECLARED = ("CLIENT-DECLARED", AMBER)


def build_appendix_d_pdf(
    company_name: str,
    qa_items: Optional[List[Dict[str, Any]]] = None,
    vendor_ctx: Optional[Dict[str, Any]] = None,
    intake: Optional[Dict[str, Any]] = None,
    acra_live: Optional[Dict[str, Any]] = None,
    compliance_score: Optional[int] = None,
    tx_hash: Optional[str] = None,
    report_id: Optional[str] = None,
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

        company = company_name or vendor_ctx.get("acra_name") or "Your Organisation"
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
                story.append(_appendix_item(
                    f"D.{i}",
                    question,
                    _xml_escape(answer),
                    *badge,
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
        logger.error(f"Appendix D PDF generation failed: {e}")
        return b""
