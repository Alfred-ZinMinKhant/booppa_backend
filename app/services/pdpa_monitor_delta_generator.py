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

from app.services.pdf_service import NAVY, EMERALD, SLATE, LIGHT_BG, BORDER, TEXT_DARK, WHITE, get_booppa_styles, get_booppa_doc_template, draw_booppa_page

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

PDPA_MONITOR_REPORT_SCHEMA_VERSION = 2  # +drift chart, +urgency alert box


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _drift_chart(score_history: List[Dict[str, Any]]):
    """A compliance-score-over-time line chart (Drawing) or None.

    `score_history` is an ordered list of {"label": "Apr", "score": 53}. Needs at
    least 3 points before we call it a "trend" — two readings are a single segment,
    not a trend, and plotting them as one overstates the signal on a board update.
    Returns None below 3 points so the caller can show an honest interim state.
    """
    pts = [(h.get("label") or "", h.get("score")) for h in (score_history or [])
           if isinstance(h.get("score"), (int, float))]
    if len(pts) < 3:
        return None
    try:
        from reportlab.graphics.shapes import Drawing
        from reportlab.graphics.charts.linecharts import HorizontalLineChart

        d = Drawing(440, 170)
        lc = HorizontalLineChart()
        lc.x, lc.y, lc.width, lc.height = 35, 25, 390, 125
        lc.data = [[p[1] for p in pts]]
        lc.categoryAxis.categoryNames = [str(p[0]) for p in pts]
        lc.valueAxis.valueMin = 0
        lc.valueAxis.valueMax = 100
        lc.valueAxis.valueStep = 20
        lc.lines[0].strokeColor = colors.HexColor("#1d4ed8")
        lc.lines[0].strokeWidth = 2
        lc.lines.symbol = None
        d.add(lc)
        return d
    except Exception as exc:  # pragma: no cover - chart is best-effort
        logger.warning("[MonitorReport] drift chart render failed: %s", exc)
        return None


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
      urgent_findings: list of {label, days_open, severity} — HIGH findings open
                       >14 days (rendered as a red alert box at the top)
      score_history: ordered list of {label, score} for the drift line chart
    """
    s = get_booppa_styles()
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

    # ── Urgency alert (6c) — HIGH findings open >14 days, at the top ──────────
    urgent: List[Dict[str, Any]] = data.get("urgent_findings") or []
    if urgent:
        alert_lines = []
        for u in urgent[:6]:
            days = int(u.get("days_open") or 0)
            label = _xml_escape(u.get("label") or "Finding")
            esc = ("After 30+ days, PDPC inspections typically begin with a review of "
                   "findings reported in prior scans.") if days >= 30 else ""
            alert_lines.append(
                f'<b>{label}</b> — open for <b>{days} days</b> — action overdue. {esc}'
            )
        alert_html = "<br/>".join(alert_lines)
        alert_tbl = Table(
            [[Paragraph(
                f'<font color="#991b1b"><b>URGENT — UNRESOLVED HIGH-RISK FINDINGS</b></font><br/>{alert_html}',
                s["body"])]],
            colWidths=[6.4 * inch],
        )
        alert_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#dc2626")),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(alert_tbl)
        story.append(Spacer(1, 14))

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
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
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
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
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

    # ── Compliance trend (6b) — line chart from month 2 onward ───────────────
    story.append(Paragraph("Compliance Trend", s["h2"]))
    _chart = _drift_chart(data.get("score_history"))
    if _chart is not None:
        story.append(_chart)
        if prev is not None and cur is not None:
            _d = int(cur) - int(prev)
            if _d > 0:
                story.append(Paragraph(
                    f'<font color="#065f46">Score improved by {_d} point(s) since last month.</font>', s["body"]))
            elif _d < 0:
                story.append(Paragraph(
                    f'<font color="#dc2626">Score declined by {abs(_d)} point(s) since last month — see findings.</font>', s["body"]))
    else:
        # <3 real points: don't draw a line that reads as a "trend". Tell the
        # reader honestly how many readings exist and when the chart appears.
        _n_pts = len([h for h in (data.get("score_history") or [])
                      if isinstance(h.get("score"), (int, float))])
        if _n_pts >= 2:
            story.append(Paragraph(
                "Two readings so far. Your compliance trend chart plots from your "
                "next scan, once there are three monthly data points.",
                s["body"]))
        else:
            story.append(Paragraph(
                "Baseline established — your compliance trend chart appears once you "
                "have three monthly scans.",
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

    doc.build(story, onFirstPage=draw_booppa_page, onLaterPages=draw_booppa_page)
    return buf.getvalue()
