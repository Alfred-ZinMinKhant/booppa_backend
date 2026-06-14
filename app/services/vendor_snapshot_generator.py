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
    story.append(Spacer(1, 18))

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

    doc.build(story)
    return buf.getvalue()
