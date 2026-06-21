"""Vendor status snapshot — tangible Vendor Active / Vendor Pro deliverable.

Vendor Active (SGD 39/mo) and Vendor Pro (SGD 99/mo) previously delivered only
a metrics email each month — no artifact the vendor could file, forward to a
procurer, or attach to a tender (a forensic-audit finding: "Deliverable: 1-line
email"). This renders a one-page status snapshot PDF from the vendor's current
scores + activity so the monthly email links a real document.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_logo import draw_logo_header

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

VENDOR_SNAPSHOT_SCHEMA_VERSION = 1


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("vs_title", parent=base["Title"], fontSize=20,
                                textColor=colors.HexColor("#0f172a"), spaceAfter=4),
        "sub": ParagraphStyle("vs_sub", parent=base["Normal"], fontSize=10,
                              textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle("vs_h2", parent=base["Heading2"], fontSize=13,
                            textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("vs_body", parent=base["Normal"], fontSize=9.5,
                              textColor=colors.HexColor("#334155"), leading=14),
        "metric": ParagraphStyle("vs_metric", parent=base["Normal"], fontSize=22,
                                textColor=colors.HexColor("#0f172a"), leading=24),
        "metric_lbl": ParagraphStyle("vs_metric_lbl", parent=base["Normal"], fontSize=8,
                                    textColor=colors.HexColor("#64748b"), leading=11),
        "small": ParagraphStyle("vs_small", parent=base["Normal"], fontSize=7.5,
                              textColor=colors.HexColor("#64748b"), leading=10),
    }


def _metric_card(s, value: str, label: str) -> List:
    return [Paragraph(value, s["metric"]), Paragraph(label, s["metric_lbl"])]


def generate_vendor_snapshot_pdf(data: Dict[str, Any]) -> bytes:
    """Render the one-page snapshot.

    Expected `data`:
      company_name: str
      plan_label:   str   (e.g. "Vendor Pro")
      generated_at: ISO/display str (optional)
      trust_score:  int|None
      compliance_score: int|None
      profile_views_30d: int|None
      verification_level: str|None
      extra_rows:   list of (label, value) appended to the details table (optional)
      trend:        {total_delta, compliance_delta} (optional) — vs last cycle
      sector_benchmark: {sector, percentile} (optional)
      tender_matches: list of {title, closing_date, bid_label} (optional)
    """
    s = _styles()
    company = data.get("company_name") or "Your Company"
    plan_label = data.get("plan_label") or "Vendor"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    def _num(v):
        return "—" if v is None else str(v)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"Vendor Status Snapshot — {company}",
    )
    story: list = []

    story.append(Paragraph("Vendor Status Snapshot", s["title"]))
    story.append(Paragraph(_xml_escape(company), s["sub"]))
    story.append(Paragraph(
        f"{_xml_escape(plan_label)} &middot; As of {gen_at} &middot; {COMPANY_NAME}", s["small"]))
    story.append(Spacer(1, 16))

    # Headline metric cards
    cards = [[
        _metric_card(s, f"{_num(data.get('trust_score'))}", "TRUST SCORE / 100"),
        _metric_card(s, f"{_num(data.get('compliance_score'))}", "COMPLIANCE SCORE / 100"),
        _metric_card(s, f"{_num(data.get('profile_views_30d'))}", "PROFILE VIEWS (30D)"),
    ]]
    card_table = Table(cards, colWidths=[2.2 * inch, 2.2 * inch, 2.0 * inch])
    card_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(card_table)
    story.append(Spacer(1, 12))

    # Trend vs last cycle + sector standing — turns flat numbers into signal.
    def _delta_txt(d, label):
        if d is None:
            return None
        if d > 0:
            return f'<font color="#16a34a">▲ {d}</font> {label} vs last cycle'
        if d < 0:
            return f'<font color="#dc2626">▼ {abs(d)}</font> {label} vs last cycle'
        return f'{label}: no change vs last cycle'

    trend = data.get("trend") or {}
    bench = data.get("sector_benchmark") or {}
    trend_bits = [
        t for t in (
            _delta_txt(trend.get("total_delta"), "Trust"),
            _delta_txt(trend.get("compliance_delta"), "Compliance"),
        ) if t
    ]
    if bench.get("percentile") is not None and bench.get("sector"):
        pct = int(bench["percentile"])
        trend_bits.append(
            f'Sector standing: <b>top {max(1, 100 - pct)}%</b> in '
            f'{_xml_escape(bench["sector"])} (ahead of {pct}% of peers)'
        )
    if trend_bits:
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(trend_bits), s["body"]))
        story.append(Spacer(1, 14))

    # Personalised tender matches (BID/WATCH/PASS).
    matches = data.get("tender_matches") or []
    if matches:
        story.append(Paragraph("Tender matches — should you bid?", s["h2"]))
        m_rows = [["Tender", "Closes", "Signal"]]
        for m in matches[:5]:
            cd = m.get("closing_date")
            close = cd.strftime("%d %b %Y") if hasattr(cd, "strftime") else (str(cd) if cd else "—")
            m_rows.append([
                Paragraph(_xml_escape((m.get("title") or "")[:80]), s["body"]),
                Paragraph(close, s["body"]),
                Paragraph(_xml_escape(m.get("bid_label") or "—"), s["body"]),
            ])
        m_tbl = Table(m_rows, colWidths=[3.9 * inch, 1.4 * inch, 1.1 * inch])
        m_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(m_tbl)
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "Signals are data-driven guidance from real GeBIZ history, not guarantees.", s["small"]))
        story.append(Spacer(1, 14))

    story.append(Paragraph("Profile Details", s["h2"]))
    rows: List[Tuple[str, str]] = [
        ("Verification Level", str(data.get("verification_level") or "Standard")),
        ("Plan", plan_label),
        ("Snapshot Date", gen_at),
    ]
    for r in (data.get("extra_rows") or []):
        try:
            rows.append((str(r[0]), str(r[1])))
        except Exception:
            continue
    detail = Table(
        [[Paragraph(f"<b>{_xml_escape(k)}</b>", s["body"]), Paragraph(_xml_escape(v), s["body"])]
         for k, v in rows],
        colWidths=[2.0 * inch, 4.4 * inch],
    )
    detail.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(detail)
    story.append(Spacer(1, 18))

    story.append(Paragraph(
        "This snapshot reflects your BOOPPA profile standing at the date shown. Scores update "
        "as you add evidence, complete scans, and as procurers view your verified profile. Keep "
        "your profile active to maintain visibility in procurement searches.",
        s["body"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"Generated by {COMPANY_NAME} for informational purposes only. Not a statement of "
        "regulatory compliance.", s["small"]))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
