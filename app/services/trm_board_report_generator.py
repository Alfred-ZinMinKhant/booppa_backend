"""MAS TRM monthly board-ready report — Standard (co-brand) vs Pro (white-label).

The Suite's board-facing deliverable: a one-page, non-technical status report the
CISO/board can read in a minute — overall compliance, RAG status per domain,
month-over-month progress, the top open risks, and next month's focus. Pro Suite
renders it white-label (the client's colours + header/footer, optional logo);
Standard renders it Booppa co-branded.

Pure function (no DB / no S3): the caller resolves controls, deltas and any
white-label logo bytes, then passes them in — so this is cheap to unit test.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.company import COMPANY_NAME
from app.services.pdf_logo import draw_logo_header

logger = logging.getLogger(__name__)

TRM_BOARD_REPORT_SCHEMA_VERSION = 1

# status → (RAG label, colour). not_started/in_progress are Amber (work owed);
# gap is Red; compliant is Green.
_RAG = {
    "compliant": ("GREEN", "#065f46"),
    "in_progress": ("AMBER", "#b45309"),
    "not_started": ("AMBER", "#b45309"),
    "gap": ("RED", "#dc2626"),
}
_STATUS_LABEL = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "compliant": "Compliant",
    "gap": "Gap Identified",
}


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def board_data_from_controls(controls, sector: str | None) -> dict:
    """Shape TrmControl rows (or dicts) into the board-report inputs.

    Returns {domains, compliant_pct, top_risks, next_focus}. `domains` is ordered
    by sector criticality so the board sees the material domains first; top_risks
    are open high/critical controls; next_focus is the highest-priority domain not
    yet compliant. Pure — no DB.
    """
    from app.services.trm_sector_override import reorder_controls_by_sector

    def _get(c, attr):
        return c.get(attr) if isinstance(c, dict) else getattr(c, attr, None)

    ordered = reorder_controls_by_sector(list(controls), sector)
    domains = [
        {
            "domain": _get(c, "domain"),
            "status": (_get(c, "status") or "not_started"),
            "risk_rating": _get(c, "risk_rating"),
        }
        for c in ordered
    ]
    total = len(domains) or 1
    compliant = sum(1 for d in domains if (d["status"] or "").lower() == "compliant")
    compliant_pct = round(100 * compliant / total)

    top_risks = [
        f"{d['domain']} — {(d['risk_rating'] or 'high').lower()} risk, "
        f"{'gap identified' if (d['status'] or '').lower() == 'gap' else 'not yet established'}"
        for d in domains
        if (d["risk_rating"] or "").lower() in ("high", "critical")
        and (d["status"] or "").lower() != "compliant"
    ]
    next_focus = None
    for d in domains:  # already in sector-priority order
        if (d["status"] or "").lower() != "compliant":
            next_focus = f"Establish and evidence the {d['domain']} domain."
            break

    return {
        "domains": domains,
        "compliant_pct": compliant_pct,
        "top_risks": top_risks,
        "next_focus": next_focus,
    }


def generate_trm_board_report_pdf(data: Dict[str, Any]) -> bytes:
    """Render the monthly board report.

    Expected `data`:
      company_name: str
      plan_label:   str            ("Standard Suite" | "Pro Suite")
      generated_at: display str (optional)
      domains:      list of {domain, status, risk_rating}
      compliant_pct: int
      previous_pct:  int | None    (None on the first cycle)
      top_risks:    list[str]      (open high/critical controls; optional)
      next_focus:   str | None
      white_label:  dict | None    Pro only:
                      {primary_color, secondary_color, footer_text,
                       report_header_text, logo_bytes(optional)}
    """
    base = getSampleStyleSheet()
    wl = data.get("white_label") or None
    primary = (wl or {}).get("primary_color") or "#0f172a"
    accent = (wl or {}).get("secondary_color") or "#10b981"

    s = {
        "title": ParagraphStyle("bt", parent=base["Title"], fontSize=19,
                                textColor=colors.HexColor(primary), spaceAfter=4),
        "sub": ParagraphStyle("bs", parent=base["Normal"], fontSize=10,
                              textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle("bh", parent=base["Heading2"], fontSize=13,
                            textColor=colors.HexColor(primary), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("bb", parent=base["Normal"], fontSize=9.5,
                              textColor=colors.HexColor("#334155"), leading=14),
        "big": ParagraphStyle("bg", parent=base["Normal"], fontSize=26,
                             textColor=colors.HexColor(primary), leading=28),
        "lbl": ParagraphStyle("bl", parent=base["Normal"], fontSize=8,
                            textColor=colors.HexColor("#64748b"), leading=11),
        "cell": ParagraphStyle("bc", parent=base["Normal"], fontSize=8.5, leading=11),
        "small": ParagraphStyle("bsm", parent=base["Normal"], fontSize=7.5,
                              textColor=colors.HexColor("#64748b"), leading=10),
    }

    company = data.get("company_name") or "Your Organisation"
    plan_label = data.get("plan_label") or "Standard Suite"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    month_label = datetime.now(timezone.utc).strftime("%B %Y")
    domains: List[Dict[str, Any]] = data.get("domains") or []
    cur = int(data.get("compliant_pct") or 0)
    prev = data.get("previous_pct")
    top_risks: List[str] = data.get("top_risks") or []
    next_focus = data.get("next_focus")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"MAS TRM Board Report — {company}",
    )
    story: list = []

    # Header: Pro white-label band (client logo/header) vs Booppa co-brand.
    on_page = None
    if wl:
        logo_bytes = wl.get("logo_bytes")
        if logo_bytes:
            try:
                story.append(Image(BytesIO(logo_bytes), width=1.8 * inch, height=0.6 * inch, kind="proportional"))
                story.append(Spacer(1, 6))
            except Exception:
                pass
        header_text = wl.get("report_header_text") or company
        band = Table([[Paragraph(f'<font color="#ffffff"><b>{_xml_escape(header_text)}</b></font>', s["sub"])]],
                     colWidths=[6.4 * inch])
        band.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(primary)),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(band)
        story.append(Spacer(1, 10))
    else:
        on_page = draw_logo_header  # Booppa co-brand

    story.append(Paragraph(f"MAS TRM Board Report — {month_label}", s["title"]))
    story.append(Paragraph(f"{_xml_escape(company)} &middot; {_xml_escape(plan_label)}", s["sub"]))
    story.append(Paragraph(f"Generated {gen_at}", s["small"]))
    story.append(Spacer(1, 14))

    # Score card: overall compliance + month-over-month delta.
    if prev is None:
        delta_disp, delta_color = "Baseline", "#64748b"
        delta_line = "First board cycle — month-over-month progress tracking begins next month."
    else:
        d = cur - int(prev)
        if d > 0:
            delta_disp, delta_color = f"▲ +{d}%", "#065f46"
            delta_line = f"Compliance improved {d} point(s) since last month."
        elif d < 0:
            delta_disp, delta_color = f"▼ {d}%", "#dc2626"
            delta_line = f"Compliance fell {abs(d)} point(s) since last month — see open risks."
        else:
            delta_disp, delta_color = "— 0%", "#64748b"
            delta_line = "No change in overall compliance since last month."

    card = Table([[
        [Paragraph(f"{cur}%", s["big"]), Paragraph("OVERALL TRM COMPLIANCE", s["lbl"])],
        [Paragraph(f'<font color="{delta_color}">{delta_disp}</font>', s["big"]),
         Paragraph("VS LAST MONTH", s["lbl"])],
    ]], colWidths=[3.2 * inch, 3.2 * inch])
    card.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(card)
    story.append(Spacer(1, 8))
    story.append(Paragraph(delta_line, s["body"]))
    story.append(Spacer(1, 6))

    # Executive summary (non-technical).
    story.append(Paragraph("Executive Summary", s["h2"]))
    greens = sum(1 for d in domains if (d.get("status") or "").lower() == "compliant")
    reds = sum(1 for d in domains if (d.get("status") or "").lower() == "gap")
    story.append(Paragraph(
        f"Across the 13 MAS TRM domains, {greens} are compliant (Green) and "
        f"{reds} have an identified gap (Red); the remainder are in progress or "
        f"not yet started (Amber). Overall control coverage stands at {cur}%.",
        s["body"]))
    story.append(Spacer(1, 6))

    # RAG status per domain.
    story.append(Paragraph("Status by Domain (RAG)", s["h2"]))
    rows = [[Paragraph("<b>MAS TRM Domain</b>", s["cell"]),
             Paragraph("<b>Status</b>", s["cell"]),
             Paragraph("<b>RAG</b>", s["cell"])]]
    for d in domains:
        st = (d.get("status") or "not_started").lower()
        rag_label, rag_color = _RAG.get(st, ("AMBER", "#b45309"))
        rows.append([
            Paragraph(_xml_escape(d.get("domain") or "—"), s["cell"]),
            Paragraph(_STATUS_LABEL.get(st, st), s["cell"]),
            Paragraph(f'<font color="{rag_color}"><b>{rag_label}</b></font>', s["cell"]),
        ])
    tbl = Table(rows, colWidths=[3.9 * inch, 1.6 * inch, 0.9 * inch], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(primary)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 12))

    # Top open risks + next focus.
    story.append(Paragraph("Top Open Risks", s["h2"]))
    if top_risks:
        for r in top_risks[:3]:
            story.append(Paragraph(f"&bull; {_xml_escape(r)}", s["body"]))
    else:
        story.append(Paragraph("No open high/critical-risk controls this cycle.", s["body"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Recommended Focus Next Month", s["h2"]))
    story.append(Paragraph(_xml_escape(next_focus) if next_focus
                           else "Maintain compliant controls and evidence the in-progress domains.", s["body"]))
    story.append(Spacer(1, 14))

    footer = (wl or {}).get("footer_text") if wl else None
    story.append(Paragraph(
        _xml_escape(footer) if footer else
        f"Prepared by {COMPANY_NAME} for board reporting. Reflects the organisation's "
        "MAS TRM workspace at generation time; not a statement of regulatory compliance.",
        s["small"]))

    if on_page:
        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    else:
        doc.build(story)
    return buf.getvalue()
