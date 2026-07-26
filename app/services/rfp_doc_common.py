"""Shared scaffolding for the two RFP-kit appendix PDFs.

``rfp_declaration_generator`` (Supplier Compliance Declaration) and
``rfp_appendix_d_generator`` (generic data-protection appendix) were near-clones:
their first ~160 lines differed only in a style-name prefix, the header label,
the footer string, and whether the item card renders an evidence line.

Geometry, the logo and the page furniture now come from :mod:`pdf_layout`; what
remains genuinely shared — the styles, the badge, and the item card — lives
here. Both documents are anchored on-chain, so their layout must stay identical:
one shared renderer is the only way that stays true.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from app.services import pdf_layout as _pl
from app.services.pdf_layout import (
    BORDER, CONTENT_W, EMERALD, LIGHT, NAVY, SLATE, WHITE,
    keep_together_safe, xml_escape,
)

BLUE = colors.HexColor("#0284c7")
AMBER = colors.HexColor("#d97706")
AMBER_BG = colors.HexColor("#fffbeb")

# Badge presentation, shared so the two documents can never disagree about what
# "verified" looks like.
VERIFIED = ("VERIFIED — BOOPPA", EMERALD)
DECLARED = ("CLIENT-DECLARED", AMBER)


def make_styles(prefix: str) -> Dict[str, ParagraphStyle]:
    """The document's paragraph styles, namespaced by ``prefix``.

    ReportLab keys styles by name globally within a stylesheet, so the two
    generators need distinct names for what is otherwise the same style set.
    """
    return {
        "normal": ParagraphStyle(f"{prefix}normal", fontSize=8, leading=12, textColor=colors.HexColor("#334155")),
        "h1":     ParagraphStyle(f"{prefix}h1", fontSize=16, leading=20, textColor=NAVY, fontName="Helvetica-Bold"),
        "h2":     ParagraphStyle(f"{prefix}h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
        "small":  ParagraphStyle(f"{prefix}small", fontSize=7, leading=10, textColor=SLATE),
        "item_t": ParagraphStyle(f"{prefix}item_t", fontSize=9, leading=12, textColor=NAVY, fontName="Helvetica-Bold"),
        "item_b": ParagraphStyle(f"{prefix}item_b", fontSize=8, leading=11, textColor=colors.HexColor("#334155")),
        "badge":  ParagraphStyle(f"{prefix}badge", fontSize=6, leading=8, textColor=WHITE, fontName="Helvetica-Bold", alignment=1),
        "warn":   ParagraphStyle(f"{prefix}warn", fontSize=7.5, leading=11, textColor=AMBER, fontName="Helvetica-Bold"),
    }


def badge(text: str, color, styles: Dict[str, ParagraphStyle]) -> Paragraph:
    style = ParagraphStyle(
        f"{id(styles)}_b_{text.replace(' ', '_')}",
        parent=styles["badge"], backColor=color, borderPadding=2,
    )
    return Paragraph(text, style)


def kv_table(rows, styles: Dict[str, ParagraphStyle]) -> Table:
    """Label/value table — the shared one; it escapes values for us."""
    return _pl.kv_table(rows)


def item_card(
    code: str,
    title: str,
    body: str,
    badge_text: str,
    badge_color,
    styles: Dict[str, ParagraphStyle],
    *,
    separator: str = ". ",
    evidence: Optional[Sequence[str]] = None,
) -> list:
    """One D-item card: code + title + status badge, body underneath.

    Returns a *list* of flowables. Previously this returned
    ``KeepTogether([header, Table([[body]]), ...])`` — doubly unbreakable, since
    a one-row table cannot split either. A long supplier answer was therefore
    silently truncated at the page boundary in a document the buyer signs and
    Booppa anchors on-chain. ``keep_together_safe`` keeps the card whole when it
    fits and lets it split when it can't, and ``splitInRow`` lets the body table
    break at a line rather than being clipped.
    """
    header = Table(
        [[
            Paragraph(f"{xml_escape(code)}{separator}{xml_escape(title)}", styles["item_t"]),
            badge(badge_text, badge_color, styles),
        ]],
        colWidths=[CONTENT_W - 1.7 * inch, 1.7 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (1, 0), (1, 0), badge_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    header.keepWithNext = 1

    elements = [Paragraph(body, styles["item_b"])]
    if evidence and badge_text == VERIFIED[0]:
        elements.append(Spacer(1, 0.05 * inch))
        for ev in evidence:
            elements.append(Paragraph(f"<i>Evidence: {xml_escape(ev)}</i>", styles["item_b"]))

    body_t = Table([[elements]], colWidths=[CONTENT_W], splitInRow=1)
    body_t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBEFORE", (0, 0), (0, -1), 2, badge_color),
    ]))
    return keep_together_safe([header, body_t, Spacer(1, 0.12 * inch)])


def build_doc(buffer, *, title: str, header_label: str, footer_text: str):
    """Doc template with the shared geometry and this document's furniture."""
    return _pl.build_doc(
        buffer, title=title, header_label=header_label,
        branding={"footer_text": footer_text},
    )
