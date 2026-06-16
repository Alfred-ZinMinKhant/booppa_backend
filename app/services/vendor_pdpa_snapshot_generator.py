"""Vendor Pro — Quarterly PDPA Snapshot with drift.

Vendor Pro (SGD 99/mo) promises in its welcome email a "Quarterly PDPA Snapshot
with drift" — but no such artifact was ever produced (forensic audit: "FILES NOT
FOUND … PDPA Quarterly Snapshot with drift for Vendor Pro — not attached").

This renders a one-page PDF that compares the vendor's latest completed PDPA scan
against the previous one: the overall compliance/risk movement, the per-dimension
status flips that worsened, and the recommended next actions. It reuses the same
drift primitives the monthly Monitor report uses
(`compliance_drift._extract_risk_score` + `_per_dimension_flips`).

When only a single scan exists (first activation), it renders a BASELINE edition
(no prior period to diff) so the "being generated now" promise still resolves with
a real document.
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

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

VENDOR_PDPA_SNAPSHOT_SCHEMA_VERSION = 1


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("vps_title", parent=base["Title"], fontSize=20,
                                textColor=colors.HexColor("#0f172a"), spaceAfter=4),
        "sub": ParagraphStyle("vps_sub", parent=base["Normal"], fontSize=10,
                              textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle("vps_h2", parent=base["Heading2"], fontSize=13,
                            textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("vps_body", parent=base["Normal"], fontSize=9.5,
                              textColor=colors.HexColor("#334155"), leading=14),
        "metric": ParagraphStyle("vps_metric", parent=base["Normal"], fontSize=22,
                                textColor=colors.HexColor("#0f172a"), leading=24),
        "metric_lbl": ParagraphStyle("vps_metric_lbl", parent=base["Normal"], fontSize=8,
                                    textColor=colors.HexColor("#64748b"), leading=11),
        "cell": ParagraphStyle("vps_cell", parent=base["Normal"], fontSize=8.5, leading=11,
                              textColor=colors.HexColor("#334155")),
        "small": ParagraphStyle("vps_small", parent=base["Normal"], fontSize=7.5,
                              textColor=colors.HexColor("#64748b"), leading=10),
    }


def _metric_card(s, value: str, label: str) -> List:
    return [Paragraph(value, s["metric"]), Paragraph(label, s["metric_lbl"])]


def _delta_label(current: Optional[int], previous: Optional[int]) -> str:
    if current is None or previous is None:
        return "—"
    d = current - previous
    if d > 0:
        return f"+{d} (improved)"
    if d < 0:
        return f"{d} (declined)"
    return "0 (no change)"


def generate_vendor_pdpa_snapshot_pdf(data: Dict[str, Any]) -> bytes:
    """Render the one-page Quarterly PDPA Snapshot with drift.

    Expected `data`:
      company_name:      str  (the CUSTOMER — never the Booppa platform name)
      scanned_url:       str|None
      generated_at:      display str (optional)
      current_score:     int|None   (compliance 0-100, higher = better)
      previous_score:    int|None
      current_risk:      float|None  (0-100, higher = worse)
      previous_risk:     float|None
      dimension_flips:   list of {dimension_name, previous_status, current_status,
                                  previous_score, current_score}
      findings_count:    int|None
      is_baseline:       bool   (no prior scan to diff against)
      anchor_tx:         str|None   (Amoy testnet tx hash, if anchored)
    """
    s = _styles()
    company = data.get("company_name") or "Your Company"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    is_baseline = bool(data.get("is_baseline"))
    cur_score = data.get("current_score")
    prev_score = data.get("previous_score")
    flips: List[Dict[str, Any]] = data.get("dimension_flips") or []

    def _num(v):
        return "—" if v is None else str(v)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"Quarterly PDPA Snapshot — {company}",
    )
    story: list = []

    edition = "Baseline edition" if is_baseline else "Drift edition"
    story.append(Paragraph("Quarterly PDPA Snapshot", s["title"]))
    story.append(Paragraph(f"<b>Vendor:</b> {_xml_escape(company)}", s["sub"]))
    if data.get("scanned_url"):
        story.append(Paragraph(f"Scanned URL: {_xml_escape(data['scanned_url'])}", s["small"]))
    story.append(Paragraph(
        f"Vendor Pro &middot; {edition} &middot; As of {gen_at}", s["small"]))
    story.append(Spacer(1, 16))

    # Headline metric cards
    cards = [[
        _metric_card(s, f"{_num(cur_score)}", "COMPLIANCE SCORE / 100"),
        _metric_card(s, f"{_num(prev_score)}", "PREVIOUS SCORE / 100"),
        _metric_card(s, _delta_label(cur_score, prev_score), "QUARTER-OVER-QUARTER"),
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

    # Dimension drift
    story.append(Paragraph("Dimension drift since last scan", s["h2"]))
    if is_baseline:
        story.append(Paragraph(
            "This is your first PDPA scan on Vendor Pro, so there is no prior period to "
            "compare against yet. Your next quarterly snapshot will show exactly which PDPA "
            "dimensions moved — and in which direction — since this baseline.", s["body"]))
    elif not flips:
        story.append(Paragraph(
            "No PDPA dimension worsened since your last scan. Every dimension held its "
            "status or improved.", s["body"]))
    else:
        header = ["PDPA Dimension", "Previous", "Now"]
        table_rows = [[Paragraph(f"<b>{h}</b>", s["cell"]) for h in header]]
        for f in flips:
            table_rows.append([
                Paragraph(_xml_escape(f.get("dimension_name", "—")), s["cell"]),
                Paragraph(_xml_escape(f.get("previous_status", "—")), s["cell"]),
                Paragraph(_xml_escape(f.get("current_status", "—")), s["cell"]),
            ])
        flip_table = Table(table_rows, colWidths=[3.4 * inch, 1.5 * inch, 1.5 * inch])
        flip_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fef2f2")]),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(flip_table)
    story.append(Spacer(1, 18))

    # Recommended actions
    story.append(Paragraph("Recommended next actions", s["h2"]))
    if flips:
        for f in flips:
            story.append(Paragraph(
                f"&bull; Remediate <b>{_xml_escape(f.get('dimension_name', ''))}</b> — it moved to "
                f"<b>{_xml_escape(f.get('current_status', ''))}</b>. Review the corresponding finding "
                "in your latest PDPA report and attach updated evidence.", s["body"]))
            story.append(Spacer(1, 3))
    else:
        for step in (
            "Keep your privacy policy, DPO contact, and retention statement current.",
            "Confirm your data-breach notification path meets PDPA §26D (notify PDPC within 3 days).",
            "Re-run your scan next quarter to keep this drift record continuous.",
        ):
            story.append(Paragraph(f"&bull; {step}", s["body"]))
            story.append(Spacer(1, 3))
    story.append(Spacer(1, 16))

    # Provenance / anchoring — honest testnet disclosure
    anchor_tx = data.get("anchor_tx")
    if anchor_tx:
        story.append(Paragraph(
            f"Integrity anchor: SHA-256 of this snapshot recorded on the Polygon <b>Amoy "
            f"testnet</b> (tx {_xml_escape(anchor_tx)}). A testnet timestamp evidences existence "
            "for tamper-checking; it does not carry the settlement guarantees of a mainnet or an "
            "accredited RFC 3161 timestamp.", s["small"]))
        story.append(Spacer(1, 8))

    story.append(Paragraph(
        f"Prepared by {_xml_escape(COMPANY_NAME)} for {_xml_escape(company)} for informational "
        "purposes only. Not a statement of regulatory compliance.", s["small"]))

    doc.build(story)
    return buf.getvalue()
