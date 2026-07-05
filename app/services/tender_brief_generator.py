"""Tender Opportunity Brief — the PDF that rides the buyer high-fit tender push.

The buyer high-fit tender push (`buyer_tender_fit_push_task`) was email-only for
every tier. This one-page brief gives it a PDF deliverable so the buyer can file
it, forward it to a procurement lead, or drop it into an evaluation folder.

Deliberately lightweight: a single page rendered from the fields the push already
carries (tender title/agency/closing/sector + matched watched-supplier names).
Reuses the shared logo header and the same styling vocabulary as
`buyer_procurement_report_generator` — no new layout primitives.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_logo import draw_logo_header
from app.services.buyer_procurement_report_generator import _xml_escape, _table, _styles

logger = logging.getLogger(__name__)

TENDER_BRIEF_SCHEMA_VERSION = 1

_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")


def generate_tender_brief_pdf(data: Dict[str, Any]) -> bytes:
    """Render a one-page Tender Opportunity Brief.

    `data` keys (all optional except tender_title):
      company_name, plan_label, tender_no, tender_title, tender_agency,
      tender_url, closing_label, sector, matched_names (list[str]), generated_at.
    """
    s = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=1.35 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title="Tender Opportunity Brief",
    )

    title = _xml_escape((data.get("tender_title") or "").strip()[:180] or "Government tender opportunity")
    agency = _xml_escape((data.get("tender_agency") or "").strip()[:120] or "Government Agency")
    sector = _xml_escape((data.get("sector") or "").strip() or "General procurement")
    closes = _xml_escape((data.get("closing_label") or "").strip() or "—")
    tender_no = _xml_escape((data.get("tender_no") or "").strip() or "—")
    url = (data.get("tender_url") or "").strip()
    company = _xml_escape((data.get("company_name") or "your organisation").strip())
    plan_label = _xml_escape((data.get("plan_label") or "Buyer").strip())
    generated_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %b %Y")

    names: List[str] = [str(n).strip() for n in (data.get("matched_names") or []) if str(n).strip()]

    story: list = []

    story.append(Paragraph("Tender Opportunity Brief", s["title"]))
    story.append(Paragraph(f"Prepared for {company} · {plan_label} · {generated_at}", s["sub"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph(title, s["h2"]))
    story.append(Spacer(1, 4))

    detail_rows = [
        [Paragraph("Field", s["cell"]), Paragraph("Detail", s["cell"])],
        [Paragraph("Tender no.", s["cell"]), Paragraph(tender_no, s["cell"])],
        [Paragraph("Agency", s["cell"]), Paragraph(agency, s["cell"])],
        [Paragraph("Sector", s["cell"]), Paragraph(sector, s["cell"])],
        [Paragraph("Closing", s["cell"]), Paragraph(closes, s["cell"])],
    ]
    story.append(_table(detail_rows, col_widths=[1.6 * inch, 5.0 * inch]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Your vetted suppliers in this sector", s["h2"]))
    if names:
        story.append(Paragraph(
            f"This tender sits in <strong>{sector}</strong> — a sector your watched "
            f"suppliers already operate in. You likely have vetted suppliers ready to "
            f"invite, benchmark, or shortlist:",
            s["body"],
        ))
        story.append(Spacer(1, 6))
        sup_rows = [[Paragraph("#", s["cell"]), Paragraph("Watched supplier", s["cell"])]]
        for i, n in enumerate(names[:12], start=1):
            sup_rows.append([Paragraph(str(i), s["cell"]), Paragraph(_xml_escape(n[:80]), s["cell"])])
        story.append(_table(sup_rows, col_widths=[0.6 * inch, 6.0 * inch]))
    else:
        story.append(Paragraph(
            f"This tender sits in <strong>{sector}</strong>, a sector on your procurement "
            f"radar. Add suppliers to your watchlist to line up vetted candidates for "
            f"opportunities like this.",
            s["body"],
        ))
    story.append(Spacer(1, 16))

    story.append(Paragraph("Suggested next steps", s["h2"]))
    for step in (
        "Confirm the requirement fit against the tender specification.",
        "Shortlist vetted suppliers from your watchlist and check their current Trust / PDPA standing.",
        "Invite or benchmark shortlisted suppliers before the closing date.",
        "Publish or respond via GeBIZ ahead of the deadline.",
    ):
        story.append(Paragraph(f"• {_xml_escape(step)}", s["body"]))
    story.append(Spacer(1, 14))

    if url:
        story.append(Paragraph(
            f'View the full tender: <a href="{_xml_escape(url)}"><font color="#0ea5e9">{_xml_escape(url[:100])}</font></a>',
            s["small"],
        ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This brief is generated from published tender metadata for evaluation "
        "convenience and is not procurement advice.",
        s["small"],
    ))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
