"""MAS TRM Baseline Assessment — tangible suite deliverable.

Standard Suite / Pro Suite activation initialises all 13 MAS TRM control
domains, but until now the buyer received only an email saying so — no
artifact. A forensic audit flagged this: "13 domains initialised" with no
baseline document to show for a SGD 1,800–4,500/mo subscription.

This module renders a one-shot baseline assessment PDF from the seeded
TrmControl rows: every domain, its control reference, its current status, and
the recommended next action. It is intentionally a STARTING-POINT document
(everything is "Not Started" on day one) — its value is giving the buyer a
structured, board-presentable inventory of what the engagement will work
through, not a finished gap analysis.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

# Bump when the visible structure of the baseline PDF changes.
TRM_BASELINE_SCHEMA_VERSION = 1

_STATUS_LABEL = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "compliant": "Compliant",
    "gap": "Gap Identified",
}
_STATUS_COLOR = {
    "not_started": "#92400e",
    "in_progress": "#1d4ed8",
    "compliant": "#065f46",
    "gap": "#dc2626",
}


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle("trm_title", parent=base["Title"], fontSize=20,
                                textColor=colors.HexColor("#0f172a"), spaceAfter=4),
        "sub": ParagraphStyle("trm_sub", parent=base["Normal"], fontSize=10,
                              textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle("trm_h2", parent=base["Heading2"], fontSize=13,
                            textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("trm_body", parent=base["Normal"], fontSize=9.5,
                              textColor=colors.HexColor("#334155"), leading=14),
        "cell": ParagraphStyle("trm_cell", parent=base["Normal"], fontSize=8.5, leading=11),
        "cell_b": ParagraphStyle("trm_cell_b", parent=base["Normal"], fontSize=8.5,
                                leading=11, textColor=colors.HexColor("#0f172a")),
        "small": ParagraphStyle("trm_small", parent=base["Normal"], fontSize=7.5,
                              textColor=colors.HexColor("#64748b"), leading=10),
    }
    return s


def generate_trm_baseline_pdf(data: Dict[str, Any]) -> bytes:
    """Render the baseline PDF.

    Expected `data`:
      company_name: str
      plan_label:   str  (e.g. "Pro Suite")
      generated_at: ISO str (optional)
      controls:     list of {domain, control_ref, status, risk_rating, gap_analysis}
    """
    s = _styles()
    company = data.get("company_name") or "Your Organisation"
    plan_label = data.get("plan_label") or "Suite"
    controls: List[Dict[str, Any]] = data.get("controls") or []
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title=f"MAS TRM Baseline — {company}",
    )
    story: list = []

    story.append(Paragraph("MAS TRM Baseline Assessment", s["title"]))
    story.append(Paragraph(_xml_escape(company), s["sub"]))
    story.append(Paragraph(
        f"{_xml_escape(plan_label)} &middot; Generated {gen_at} &middot; {COMPANY_NAME}",
        s["small"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Scope", s["h2"]))
    story.append(Paragraph(
        "This baseline inventories all 13 control domains of the Monetary Authority of "
        "Singapore (MAS) Technology Risk Management (TRM) Guidelines as initialised for your "
        "organisation. Every domain begins at <b>Not Started</b>; work each one in your TRM "
        "workspace, run the AI gap analysis, and attach evidence to move a domain to "
        "<b>Compliant</b>. This document is a structured starting point, not a statement of "
        "compliance.",
        s["body"]))
    story.append(Spacer(1, 6))

    # Status summary
    counts: Dict[str, int] = {}
    for c in controls:
        st = (c.get("status") or "not_started")
        counts[st] = counts.get(st, 0) + 1
    summary = " &middot; ".join(
        f"{_STATUS_LABEL.get(k, k)}: {v}" for k, v in sorted(counts.items())
    ) or "No controls initialised"
    story.append(Paragraph(f"<b>Summary:</b> {summary} (of {len(controls)} domains)", s["body"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Control Domains", s["h2"]))
    header = [
        Paragraph("<b>Ref</b>", s["cell_b"]),
        Paragraph("<b>MAS TRM Domain</b>", s["cell_b"]),
        Paragraph("<b>Status</b>", s["cell_b"]),
        Paragraph("<b>Next Action</b>", s["cell_b"]),
    ]
    rows = [header]
    status_row_styles = []
    for i, c in enumerate(controls, start=1):
        st = (c.get("status") or "not_started")
        color_hex = _STATUS_COLOR.get(st, "#334155")
        status_para = Paragraph(
            f'<font color="{color_hex}">{_STATUS_LABEL.get(st, st)}</font>', s["cell"],
        )
        next_action = (
            "Run AI gap analysis & attach evidence" if st == "not_started"
            else "Complete in-progress evidence" if st == "in_progress"
            else "Remediate identified gap" if st == "gap"
            else "Maintain & re-attest"
        )
        rows.append([
            Paragraph(_xml_escape(c.get("control_ref") or f"TRM-{i}"), s["cell"]),
            Paragraph(_xml_escape(c.get("domain") or "—"), s["cell"]),
            status_para,
            Paragraph(next_action, s["cell"]),
        ])

    table = Table(rows, colWidths=[0.7 * inch, 2.9 * inch, 1.1 * inch, 2.2 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Recommended First Steps", s["h2"]))
    for step in (
        "Prioritise Cyber Security (TRM-5), Authentication &amp; Access Management (TRM-13), and "
        "Incident Management (TRM-8) — these carry the highest supervisory attention.",
        "Use the AI gap analysis in your TRM workspace to draft a gap narrative and risk rating per domain.",
        "Attach existing policies and evidence to each control to move it toward Compliant.",
        "Re-generate this baseline any time to track how many domains have advanced.",
    ):
        story.append(Paragraph(f"&bull; {step}", s["body"]))
        story.append(Spacer(1, 3))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"This document is generated by {COMPANY_NAME} for informational "
        "purposes only and does not constitute legal or regulatory advice or a statement of MAS "
        "compliance.", s["small"]))

    doc.build(story)
    return buf.getvalue()
