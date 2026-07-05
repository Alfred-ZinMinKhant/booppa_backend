"""
Buyer Essentials — Welcome Pack Generator
=========================================
A branded ReportLab PDF emailed to a buyer subscriber on their first cycle
(subscription activation). Unlike the Procurement Intelligence Report (which is
built from live watchlist data), this is a static onboarding artifact: it tells
the buyer exactly what their plan includes and how to use each capability.

Sections:
  1. Cover / Welcome
  2. What's included (the four capabilities)
  3. Vendor Scans — quota + how to scan
  4. Compliance Dashboard — traffic-light + alerts
  5. Vendor Directory — browse + filter the network
  6. Exports — CSV / PDF for tender spreadsheets
  7. Getting started checklist

Scaffolding (BaseDocTemplate/Frame/_draw_page/_section/_kv_table/_STYLES/
_xml_escape) mirrors cover_sheet_generator.py so both deliverables share one
visual system.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Logo resolution mirrors cover_sheet_generator.py / pdf_service.py so all three
# generators find the same asset regardless of source-tree vs container layout.
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

# Bump when the visible structure of the welcome pack changes.
WELCOME_PACK_SCHEMA_VERSION = 1

# Monthly QUICK-scan quota for Buyer Essentials (buyer_starter). Mirrors
# BUYER_SCAN_LIMITS["buyer_starter"] QUICK in app/billing/enforcement.py — kept
# as a display constant, not a runtime source of truth.
ESSENTIALS_SCAN_QUOTA = 10

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


def _draw_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    # Explicit width — drawImage with only `height` silently skips on some
    # ReportLab builds. Logo is ~423×144 px → aspect ~2.94.
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
        "BUYER ESSENTIALS · WELCOME PACK",
    )

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, "Booppa Smart Care LLC · booppa.io · Confidential")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


_RAW_STYLES = getSampleStyleSheet()
_STYLES: Dict[str, ParagraphStyle] = {
    "Normal":  ParagraphStyle("we_normal", fontSize=8.5, leading=13, textColor=colors.HexColor("#334155")),
    "h1":      ParagraphStyle("we_h1", fontSize=18, leading=22, textColor=NAVY, fontName="Helvetica-Bold"),
    "h2":      ParagraphStyle("we_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
    "small":   ParagraphStyle("we_small", fontSize=7, leading=10, textColor=SLATE),
    "caption": ParagraphStyle("we_caption", fontSize=9, leading=13, textColor=colors.HexColor("#334155")),
    "body":    ParagraphStyle("we_body", fontSize=9, leading=14, textColor=colors.HexColor("#334155"), spaceAfter=4),
}


def _xml_escape(s: str) -> str:
    """Escape user-supplied text for ReportLab's Paragraph mini-XML."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _section(title: str, *, page_break: bool = False) -> list:
    """Section header; keepWithNext on h2 prevents the title widowing."""
    out: list = []
    out.append(PageBreak() if page_break else Spacer(1, 0.15 * inch))
    out.append(Paragraph(f'<font color="#10b981">■</font>  <b>{_xml_escape(title)}</b>', _STYLES["h2"]))
    out.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6))
    return out


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    def _val(v):
        return v if isinstance(v, Paragraph) else Paragraph(_xml_escape(str(v)), _STYLES["Normal"])
    data = [[Paragraph(f"<b>{_xml_escape(k)}</b>", _STYLES["Normal"]), _val(v)] for k, v in rows]
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


def _capability_block(number: int, title: str, blurb: str, bullets: list[str]) -> KeepTogether:
    """A numbered capability card: emerald index + title, blurb, bullet list.
    KeepTogether so a card never splits across a page boundary.
    """
    head = Table(
        [[
            Paragraph(f'<font color="#ffffff"><b>{number}</b></font>',
                      ParagraphStyle("cap_idx", fontSize=11, leading=13, alignment=1, textColor=WHITE)),
            Paragraph(f"<b>{_xml_escape(title)}</b>",
                      ParagraphStyle("cap_title", fontSize=11, leading=14, textColor=NAVY, fontName="Helvetica-Bold")),
        ]],
        colWidths=[0.32 * inch, 6.38 * inch],
    )
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), EMERALD),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow: list = [head, Spacer(1, 0.04 * inch),
                  Paragraph(_xml_escape(blurb), _STYLES["body"])]
    for b in bullets:
        flow.append(Paragraph(f'<font color="#10b981">•</font>  {_xml_escape(b)}', _STYLES["body"]))
    return KeepTogether(flow)


def generate_buyer_essentials_pack(data: Dict[str, Any]) -> bytes:
    """Build and return the Buyer Essentials welcome pack PDF bytes.

    Expected keys in `data`:
      company        — buyer organisation name (str)
      buyer_email    — buyer contact email (str)
      plan_label     — display label, default "Buyer Essentials"
      scan_quota     — monthly QUICK scan quota, default ESSENTIALS_SCAN_QUOTA
    """
    company    = data.get("company") or "Your organisation"
    buyer_email = data.get("buyer_email") or ""
    plan_label = data.get("plan_label") or "Buyer Essentials"
    scan_quota = data.get("scan_quota") or ESSENTIALS_SCAN_QUOTA
    now = datetime.now(timezone.utc).strftime("%d %b %Y")

    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_H + 0.3 * inch,
        bottomMargin=FOOTER_H + 0.3 * inch,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=_draw_page)])

    story: list = []

    # ── Section 1: Cover / Welcome ────────────────────────────────────────────
    if _LOGO_PATH:
        try:
            story.append(Spacer(1, 0.05 * inch))
            story.append(Image(_LOGO_PATH, width=2.4 * inch, height=0.82 * inch, kind="proportional"))
            story.append(Spacer(1, 0.12 * inch))
        except Exception as e:
            logger.warning("[WelcomePack] Body logo render failed: %s", e)

    story.append(Paragraph(f"Welcome to {_xml_escape(plan_label)}", _STYLES["h1"]))
    story.append(Paragraph(f"Onboarding pack for {_xml_escape(company)}", _STYLES["caption"]))
    story.append(Spacer(1, 0.12 * inch))
    story.append(Paragraph(
        "Your subscription is active. This pack walks through everything your plan "
        "includes and how to put each capability to work today.",
        _STYLES["body"],
    ))

    story += _section("Your plan at a glance")
    story.append(_kv_table([
        ("Plan", plan_label),
        ("Organisation", company),
        ("Account email", buyer_email or "—"),
        ("Vendor scans", f"{scan_quota} Quick Scans per month"),
        ("Activated", now),
    ]))

    # ── Section 2: What's included ────────────────────────────────────────────
    story += _section("What's included")
    story.append(_capability_block(
        1, "Vendor Scans",
        f"Run a Quick Scan on up to {scan_quota} vendors every month. Each scan checks "
        "the vendor against the ACRA company registry, the MAS watchlist, and raises a "
        "PDPA compliance flag.",
        [
            f"{scan_quota} Quick Scans / month (re-viewing a vendor you already scanned this month is free)",
            "ACRA registration + entity status",
            "MAS watchlist screening",
            "PDPA compliance flag",
        ],
    ))
    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        2, "Compliance Dashboard",
        "A traffic-light view across every vendor you scan — CLEAN, WATCH, FLAGGED, or "
        "CRITICAL — with automatic email alerts when a vendor enters critical status.",
        [
            "At-a-glance RAG status for your whole portfolio",
            "Automatic alert when a vendor crosses your alert threshold",
            "Portfolio risk summary counts",
        ],
    ))
    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        3, "Vendor Directory",
        "Browse the Booppa vendor network with advanced filters so you can shortlist "
        "the right suppliers fast.",
        [
            "Filter by sector, size, and certifications",
            "Sort by compliance score and verification status",
            "See risk signal and trajectory before you engage",
        ],
    ))
    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        4, "Exports",
        "Export your scan results as CSV — ready to drop straight into a tender "
        "evaluation spreadsheet — or as a formatted PDF for the record.",
        [
            "CSV export of scan results for tender spreadsheets",
            "PDF export for filing and sharing",
        ],
    ))

    # ── Sections 3–6: How to use each capability ──────────────────────────────
    story += _section("Vendor Scans — how to scan", page_break=True)
    story.append(_kv_table([
        ("Monthly quota", f"{scan_quota} Quick Scans"),
        ("What a scan covers", "ACRA registry · MAS watchlist · PDPA flag"),
        ("Where", "Buyer dashboard → search a vendor → Quick Scan"),
        ("Quota reset", "1st of each calendar month"),
    ]))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Re-viewing a vendor you have already scanned this month does not consume "
        "another credit — only the first scan of each vendor per month counts.",
        _STYLES["body"],
    ))

    story += _section("Compliance Dashboard — traffic-light & alerts")
    story.append(_kv_table([
        ("CLEAN", "No open risk signals — cleared for procurement."),
        ("WATCH", "Minor signals — monitor before you commit."),
        ("FLAGGED", "Open risk signals require resolution."),
        ("CRITICAL", "Severe signals — you receive an automatic alert email."),
    ]))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Set an alert threshold per vendor and Booppa emails you the moment a watched "
        "vendor crosses it, so you never miss a status change.",
        _STYLES["body"],
    ))

    story += _section("Vendor Directory — browse & filter")
    story.append(Paragraph(
        "Open the vendor directory to explore the network. Narrow the list with filters "
        "for sector, company size, and certifications, then sort by compliance score or "
        "verification status to build a shortlist.",
        _STYLES["body"],
    ))

    story += _section("Exports — CSV & PDF")
    story.append(Paragraph(
        "From your scan results, export a CSV to pull vendor status straight into a "
        "tender evaluation spreadsheet, or export a formatted PDF for your records and "
        "to share with approvers.",
        _STYLES["body"],
    ))

    # ── Section 7: Getting started ────────────────────────────────────────────
    story += _section("Getting started")
    for step in [
        "Open your buyer dashboard and add your first suppliers to the watchlist.",
        "Run a Quick Scan on the vendors you are evaluating this month.",
        "Set alert thresholds so you're notified if a vendor turns critical.",
        "Export your results to CSV when you're ready to build a tender shortlist.",
    ]:
        story.append(Paragraph(f'<font color="#10b981">✓</font>  {_xml_escape(step)}', _STYLES["body"]))

    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        "Questions? Reply to your welcome email or reach us at booppa.io. "
        "This pack is informational and describes plan features as of the activation date.",
        _STYLES["small"],
    ))

    doc.build(story)
    return buf.getvalue()
