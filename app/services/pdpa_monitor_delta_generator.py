"""PDPA Monitor Report — month-over-month delta deliverable.

PDPA Monitor (SGD 299/mo) promised a "Monitor report" but delivered the latest
one-off Quick Scan with no month-over-month comparison (forensic-audit finding).
This renders a Monitor-specific report: the current compliance score, the change
versus the previous scan, and which dimensions moved. On the first cycle (no
prior scan) it renders a baseline edition that says deltas begin next month.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_logo import draw_logo_header

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

PDPA_MONITOR_REPORT_SCHEMA_VERSION = 1


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("pm_title", parent=base["Title"], fontSize=20,
                                textColor=colors.HexColor("#0f172a"), spaceAfter=4),
        "sub": ParagraphStyle("pm_sub", parent=base["Normal"], fontSize=10,
                              textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle("pm_h2", parent=base["Heading2"], fontSize=13,
                            textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("pm_body", parent=base["Normal"], fontSize=9.5,
                              textColor=colors.HexColor("#334155"), leading=14),
        "big": ParagraphStyle("pm_big", parent=base["Normal"], fontSize=26,
                             textColor=colors.HexColor("#0f172a"), leading=28),
        "lbl": ParagraphStyle("pm_lbl", parent=base["Normal"], fontSize=8,
                            textColor=colors.HexColor("#64748b"), leading=11),
        "cell": ParagraphStyle("pm_cell", parent=base["Normal"], fontSize=8.5, leading=11),
        "small": ParagraphStyle("pm_small", parent=base["Normal"], fontSize=7.5,
                              textColor=colors.HexColor("#64748b"), leading=10),
    }


def generate_pdpa_monitor_report_pdf(data: Dict[str, Any]) -> bytes:
    """Render the Monitor report.

    Expected `data`:
      company_name: str
      generated_at: display str (optional)
      current_score: int|None       (compliance score, higher = better)
      previous_score: int|None      (None on first cycle)
      scanned_url: str|None
      findings_count: int|None
      dimension_changes: list of {dimension_name, previous_status, current_status}
      full_report_url: str|None
    """
    s = _styles()
    company = data.get("company_name") or "Your Organisation"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    month_label = datetime.now(timezone.utc).strftime("%B %Y")
    cur = data.get("current_score")
    prev = data.get("previous_score")
    changes: List[Dict[str, Any]] = data.get("dimension_changes") or []

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"PDPA Monitor Report — {company}",
    )
    story: list = []

    story.append(Paragraph(f"PDPA Monitor Report — {month_label}", s["title"]))
    story.append(Paragraph(_xml_escape(company), s["sub"]))
    url = data.get("scanned_url")
    meta = f"Generated {gen_at} &middot; {COMPANY_NAME}"
    if url:
        meta = f"Scanned {_xml_escape(url)} &middot; " + meta
    story.append(Paragraph(meta, s["small"]))
    story.append(Spacer(1, 16))

    # Score + change
    cur_disp = "—" if cur is None else str(int(cur))
    if prev is None:
        change_line = ("This is your first monitoring cycle — your baseline compliance score. "
                       "Month-over-month change tracking begins with next month's scan.")
        change_color = "#64748b"
        delta_disp = "Baseline"
    else:
        delta = (int(cur) - int(prev)) if cur is not None else 0
        if delta > 0:
            change_line = f"Up {delta} point(s) since last month — compliance improved."
            change_color = "#065f46"
            delta_disp = f"▲ +{delta}"
        elif delta < 0:
            change_line = f"Down {abs(delta)} point(s) since last month — review the regressions below."
            change_color = "#dc2626"
            delta_disp = f"▼ {delta}"
        else:
            change_line = "No change in overall compliance score since last month."
            change_color = "#64748b"
            delta_disp = "— 0"

    score_card = [[
        [Paragraph(cur_disp, s["big"]), Paragraph("CURRENT COMPLIANCE / 100", s["lbl"])],
        [Paragraph(f'<font color="{change_color}">{delta_disp}</font>', s["big"]),
         Paragraph("CHANGE VS LAST MONTH", s["lbl"])],
        [Paragraph("—" if prev is None else str(int(prev)), s["big"]),
         Paragraph("PREVIOUS / 100", s["lbl"])],
    ]]
    card = Table(score_card, colWidths=[2.3 * inch, 2.3 * inch, 1.8 * inch])
    card.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(card)
    story.append(Spacer(1, 10))
    story.append(Paragraph(change_line, s["body"]))
    story.append(Spacer(1, 8))
    fc = data.get("findings_count")
    if fc is not None:
        story.append(Paragraph(f"<b>Open findings this scan:</b> {int(fc)}", s["body"]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Dimension Changes", s["h2"]))
    if changes:
        rows = [[Paragraph("<b>Dimension</b>", s["cell"]),
                 Paragraph("<b>Was</b>", s["cell"]),
                 Paragraph("<b>Now</b>", s["cell"])]]
        for c in changes:
            rows.append([
                Paragraph(_xml_escape(c.get("dimension_name") or "—"), s["cell"]),
                Paragraph(_xml_escape(c.get("previous_status") or "—"), s["cell"]),
                Paragraph(_xml_escape(c.get("current_status") or "—"), s["cell"]),
            ])
        t = Table(rows, colWidths=[3.4 * inch, 1.5 * inch, 1.5 * inch], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
    else:
        story.append(Paragraph(
            "No dimension status changes detected since the previous scan." if prev is not None
            else "Dimension-level change tracking begins from your next scan.",
            s["body"]))
    story.append(Spacer(1, 14))

    if data.get("full_report_url"):
        story.append(Paragraph(
            f'Full detailed findings: <a href="{data["full_report_url"]}">'
            f'<font color="#1d4ed8">download the complete PDPA report</font></a>.', s["body"]))
        story.append(Spacer(1, 10))

    story.append(Paragraph(
        f"Generated by {COMPANY_NAME} for informational purposes only. Reflects publicly "
        "accessible website elements at scan time; not a statement of regulatory compliance.",
        s["small"]))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
