"""Verifiable offline artefacts — exportable PDFs for dashboard-only features.

A forensic audit flagged that several paid features were "declared but not
verifiable offline" — they lived only on the dashboard, so a vendor could not
file, forward, or attach them to a tender:

  * Verification badge      → Badge Certificate PDF
  * Priority search placement → Priority Placement Report PDF
  * Competitor awareness signals (Vendor Pro) → Competitor Activity Report PDF
  * AI bid/watch/pass timing  → Bid-Timing Report PDF

Each function renders a one-page, board-presentable PDF. The assessed/owning
entity is always the CUSTOMER (never the Booppa platform name). Where a hash is
anchored the disclosure is honest about the network (Amoy testnet under Lean
Mode). All four reuse the shared style block below.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_styles import get_unified_styles
from app.core.company import COMPANY_NAME
from app.services.pdf_logo import draw_logo_header

logger = logging.getLogger(__name__)

VENDOR_ARTIFACTS_SCHEMA_VERSION = 1


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




def _doc(buf: BytesIO, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=title,
    )


def _metric_card(s, value: str, label: str) -> List:
    return [Paragraph(value, s["metric"]), Paragraph(label, s["metric_lbl"])]


def _cards_table(s, cards: List[Tuple[str, str]], widths: List[float]) -> Table:
    row = [[_metric_card(s, v, lbl) for v, lbl in cards]]
    t = Table(row, colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    return t


def _data_table(s, header: List[str], rows: List[List[str]], widths: List[float]) -> Table:
    body = [[Paragraph(f"<b>{_xml_escape(h)}</b>", s["cell"]) for h in header]]
    for r in rows:
        body.append([Paragraph(_xml_escape(str(c)), s["cell"]) for c in r])
    t = Table(body, colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _anchor_note(s, anchor_tx: Optional[str]) -> List:
    if not anchor_tx:
        return []
    return [
        Spacer(1, 8),
        Paragraph(
            f"Integrity anchor: SHA-256 of this document recorded on the Polygon "
            f"<b>Amoy testnet</b> (tx {_xml_escape(anchor_tx)}). A testnet timestamp "
            "evidences existence for tamper-checking; it does not carry the settlement "
            "guarantees of a mainnet or an accredited RFC 3161 timestamp.", s["small"]),
    ]


def _footer(s, company: str) -> Paragraph:
    return Paragraph(
        f"Prepared by {_xml_escape(COMPANY_NAME)} for {_xml_escape(company)} for informational "
        "purposes only. Not a statement of regulatory compliance.", s["small"])


# ── 1. Badge Certificate ──────────────────────────────────────────────────

_READINESS_LABEL = {
    "READY": "Procurement-Ready",
    "CONDITIONAL": "Conditionally Ready",
    "NEEDS_ATTENTION": "Needs Attention",
    "NOT_READY": "Not Yet Ready",
}


def generate_badge_certificate_pdf(data: Dict[str, Any]) -> bytes:
    """Badge Certificate — an offline, attestable version of the BOOPPA badge.

    Expected `data`: company_name, verification_depth, procurement_readiness,
    confidence_score, vendor_id, verify_url, generated_at, anchor_tx (optional).

    The wording attests IDENTITY/registration verification at the stated depth —
    it does not assert PDPA/MAS compliance (audit: badge must not mislead
    procurement officers).
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Company"
    depth = str(data.get("verification_depth") or "BASIC")
    readiness = str(data.get("procurement_readiness") or "CONDITIONAL")
    confidence = data.get("confidence_score")
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    verify_url = data.get("verify_url") or "https://www.booppa.io/verify"

    buf = BytesIO()
    story: list = []
    story.append(Paragraph("Verification Badge Certificate", s["title"]))
    story.append(Paragraph(f"<b>Vendor:</b> {_xml_escape(company)}", s["sub"]))
    story.append(Paragraph(f"Issued {gen_at}", s["small"]))
    story.append(Spacer(1, 16))

    story.append(_cards_table(s, [
        (depth, "VERIFICATION DEPTH"),
        (_READINESS_LABEL.get(readiness, readiness), "PROCUREMENT READINESS"),
        ("—" if confidence is None else f"{int(round(float(confidence)))}", "CONFIDENCE / 100"),
    ], [2.2 * inch, 2.4 * inch, 1.8 * inch]))
    story.append(Spacer(1, 18))

    story.append(Paragraph("What this badge attests", s["h2"]))
    story.append(Paragraph(
        f"BOOPPA has verified the identity and business registration of "
        f"<b>{_xml_escape(company)}</b> at the <b>{_xml_escape(depth)}</b> level. This certificate "
        "attests that verification — it is <b>not</b> a statement of PDPA or MAS compliance. "
        "Procurement readiness and confidence are derived from the vendor's latest completed "
        "compliance assessment, not from payment or plan tier.", s["body"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Independent verification", s["h2"]))
    story.append(Paragraph(
        f"Confirm this badge live at: {_xml_escape(verify_url)}", s["body"]))
    story.extend(_anchor_note(s, data.get("anchor_tx")))
    story.append(Spacer(1, 16))
    story.append(_footer(s, company))

    _doc(buf, f"Verification Badge Certificate — {company}").build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()


# ── 2. Priority Placement Report ──────────────────────────────────────────

def generate_priority_placement_pdf(data: Dict[str, Any]) -> bytes:
    """Priority Placement Report — offline evidence of the priority-search entitlement.

    Expected `data`: company_name, plan_label, profile_views_30d, verification_depth,
    placement_active (bool), generated_at, anchor_tx (optional).
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Company"
    plan_label = data.get("plan_label") or "Vendor"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    views = data.get("profile_views_30d")
    active = bool(data.get("placement_active", True))

    buf = BytesIO()
    story: list = []
    story.append(Paragraph("Priority Placement Report", s["title"]))
    story.append(Paragraph(f"<b>Vendor:</b> {_xml_escape(company)}", s["sub"]))
    story.append(Paragraph(f"{_xml_escape(plan_label)} &middot; As of {gen_at}", s["small"]))
    story.append(Spacer(1, 16))

    story.append(_cards_table(s, [
        ("Active" if active else "Inactive", "PRIORITY PLACEMENT"),
        ("—" if views is None else str(views), "PROFILE VIEWS (30D)"),
        (str(data.get("verification_depth") or "BASIC"), "VERIFICATION DEPTH"),
    ], [2.2 * inch, 2.2 * inch, 2.0 * inch]))
    story.append(Spacer(1, 18))

    story.append(Paragraph("Your placement entitlement", s["h2"]))
    story.append(Paragraph(
        f"As an active <b>{_xml_escape(plan_label)}</b> subscriber, <b>{_xml_escape(company)}</b> "
        "receives priority placement in BOOPPA procurement searches: your verified profile is "
        "surfaced ahead of unverified vendors to buyers browsing your sectors. The profile-view "
        "count above is the measurable signal of that visibility over the last 30 days.", s["body"]))
    story.extend(_anchor_note(s, data.get("anchor_tx")))
    story.append(Spacer(1, 16))
    story.append(_footer(s, company))

    _doc(buf, f"Priority Placement Report — {company}").build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()


# ── 3. Competitor Activity Report ─────────────────────────────────────────

def generate_competitor_signals_pdf(data: Dict[str, Any]) -> bytes:
    """Competitor Activity Report — offline form of the live competitor-signals view.

    Expected `data`: company_name, tender_no, window_days, lookups
    {focal, focal_verified, similar, similar_verified}, sector,
    sector_active_verified, generated_at. All figures are anonymised counts.
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Company"
    tender_no = data.get("tender_no") or "—"
    window = data.get("window_days") or 30
    lk = data.get("lookups") or {}
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    buf = BytesIO()
    story: list = []
    story.append(Paragraph("Competitor Activity Report", s["title"]))
    story.append(Paragraph(f"<b>Vendor:</b> {_xml_escape(company)}", s["sub"]))
    story.append(Paragraph(
        f"Tender {_xml_escape(tender_no)} &middot; Last {window} days &middot; As of {gen_at}",
        s["small"]))
    story.append(Spacer(1, 16))

    story.append(_cards_table(s, [
        (str(lk.get("focal", 0)), "LOOKUPS — THIS TENDER"),
        (str(lk.get("similar", 0)), "LOOKUPS — SIMILAR TENDERS"),
        (str(data.get("sector_active_verified", 0)), "ACTIVE VERIFIED IN SECTOR"),
    ], [2.2 * inch, 2.2 * inch, 2.0 * inch]))
    story.append(Spacer(1, 18))

    story.append(Paragraph("Interest breakdown", s["h2"]))
    story.append(_data_table(s,
        ["Signal", "All vendors", "Verified vendors"],
        [
            ["This tender", lk.get("focal", 0), lk.get("focal_verified", 0)],
            ["Similar tenders", lk.get("similar", 0), lk.get("similar_verified", 0)],
        ],
        [3.0 * inch, 1.7 * inch, 1.7 * inch]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "Figures are anonymised counts of how many vendors reviewed this tender and similar "
        "tenders on BOOPPA in the window shown. No competitor identities are ever disclosed. "
        "Higher verified-vendor interest signals a more competitive bid.", s["body"]))
    story.append(Spacer(1, 16))
    story.append(_footer(s, company))

    _doc(buf, f"Competitor Activity Report — {company}").build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()


# ── 4. Bid-Timing Report ──────────────────────────────────────────────────

def generate_bid_timing_pdf(data: Dict[str, Any]) -> bytes:
    """Bid-Timing Report — standalone, downloadable form of the AI bid-timing signal.

    Expected `data`: company_name, period_label, total_awards, busiest_month,
    months [{month, awards, value}], generated_at.
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Company"
    period_label = data.get("period_label") or "Recent GeBIZ awards"
    busiest = data.get("busiest_month") or "—"
    total = data.get("total_awards") or 0
    months: List[Dict[str, Any]] = data.get("months") or []
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    buf = BytesIO()
    story: list = []
    story.append(Paragraph("Bid-Timing Report", s["title"]))
    story.append(Paragraph(f"<b>Prepared for:</b> {_xml_escape(company)}", s["sub"]))
    story.append(Paragraph(f"{_xml_escape(period_label)} &middot; As of {gen_at}", s["small"]))
    story.append(Spacer(1, 16))

    story.append(_cards_table(s, [
        (str(total), "AWARDS ANALYSED"),
        (busiest, "BUSIEST AWARD MONTH"),
        (str(len(months)), "MONTHS COVERED"),
    ], [2.0 * inch, 2.4 * inch, 2.0 * inch]))
    story.append(Spacer(1, 18))

    story.append(Paragraph("When awards land — month by month", s["h2"]))
    if months:
        rows = [[m.get("month", "—"), m.get("awards", 0), f"S${float(m.get('value', 0)):,.0f}"]
                for m in months]
        story.append(_data_table(s, ["Month", "Awards", "Total value"], rows,
                                 [2.6 * inch, 1.7 * inch, 2.1 * inch]))
    else:
        story.append(Paragraph("No award history available for this period yet.", s["body"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"<b>Timing signal:</b> historically, the most awards in this dataset landed in "
        f"<b>{_xml_escape(busiest)}</b>. Agencies tend to clear procurement budgets on a cycle — "
        "preparing bids ahead of the busiest month positions you to respond fastest. This is a "
        "data-driven signal from real GeBIZ award history, not a guarantee.", s["body"]))
    story.append(Spacer(1, 16))
    story.append(_footer(s, company))

    _doc(buf, f"Bid-Timing Report — {company}").build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
