"""Buyer Procurement Intelligence Report — the recurring buyer-subscription deliverable.

The buyer analog of `vendor_pro_report_generator`. A multi-section PDF that
consolidates what a procurement buyer's subscription monitors on their behalf into
one board-presentable artifact:

  1. Watchlist health headline (suppliers watched / alerting / slipped)
  2. Watched-supplier roster with status, Trust/Compliance score + drift
  3. Suppliers that need attention (FLAGGED / CRITICAL risk signals)
  4. New tenders to evaluate (soonest-closing open GeBIZ tenders)
  5. What your plan includes

Every section degrades gracefully when its data is missing — the report always
renders, including for a Starter buyer with an empty watchlist. Reuses the shared
logo header (`pdf_logo.draw_logo_header`) and the same styling vocabulary as
`vendor_pro_report_generator`.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_styles import get_unified_styles
from app.services.pdf_logo import draw_logo_header
from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

BUYER_PROCUREMENT_REPORT_SCHEMA_VERSION = 1

_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")
_PAPER = colors.HexColor("#f8fafc")

# What each tier's report advertises (mirrors the digest tiering).
_TIER_INCLUDES = {
    "starter": [
        "Monthly procurement intelligence digest",
        "Supplier directory + compliance scan access",
        "New GeBIZ tenders to evaluate",
    ],
    "pro": [
        "This monthly Procurement Intelligence Report (PDF)",
        "Watchlist monitoring with month-over-month drift",
        "New GeBIZ tenders to evaluate",
        "Team collaboration (shared watchlist, comments)",
        "Customisable risk-scoring weights",
    ],
    "enterprise": [
        "This monthly Procurement Intelligence Report (PDF)",
        "Full multi-supplier watchlist monitoring + drift alerts",
        "New GeBIZ tenders to evaluate",
        "Custom evaluation frameworks + RBAC seats",
        "Priority support",
    ],
}


def _xml_escape(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




def _table(rows: list, col_widths: list, header: bool = True) -> Table:
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), _INK),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _PAPER]),
        ]
    else:
        style += [("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _PAPER])]
    t.setStyle(TableStyle(style))
    return t


def _delta_cell(d, s) -> Paragraph:
    """Coloured ▲/▼ delta for a table cell. None → em dash."""
    if d is None or not isinstance(d, int):
        return Paragraph("—", s["cell"])
    if d > 0:
        return Paragraph(f'<font color="#16a34a">▲ {d}</font>', s["cell"])
    if d < 0:
        return Paragraph(f'<font color="#dc2626">▼ {abs(d)}</font>', s["cell"])
    return Paragraph("0", s["cell"])


def generate_buyer_procurement_report_pdf(data: Dict[str, Any]) -> bytes:
    """Render the consolidated buyer report. `data` keys (all optional except company_name):
      company_name, plan_label, tier ("starter"|"pro"|"enterprise"), generated_at,
      watchlist_summary: {total, alerting, slipped, alerting_names},
      watched_suppliers: [{vendor_name, resolved, risk_signal, procurement_readiness,
                           trust_score, compliance_score, trust_delta, compliance_delta}],
      tender_matches: [{title, agency, closing_date, bid_label}],
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Organisation"
    tier = (data.get("tier") or "pro").lower()
    plan_label = data.get("plan_label") or "Buyer"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    # When the watchlist is empty we substitute a fictional sample estate; that
    # MUST be unmissable in the delivered PDF so no reader mistakes it for their
    # real suppliers (a banner on every page + a first-page callout).
    sample_data = bool(data.get("sample_data"))

    def _num(v):
        return "—" if v is None else str(v)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"Procurement Intelligence Report — {company}",
    )
    story: list = []

    story.append(Paragraph("Procurement Intelligence Report", s["title"]))
    story.append(Paragraph(_xml_escape(company), s["sub"]))
    story.append(Paragraph(f"{_xml_escape(plan_label)} &middot; As of {gen_at} &middot; {COMPANY_NAME}", s["small"]))
    story.append(Spacer(1, 16))

    # First-page sample-data callout (the per-page banner is drawn by the page
    # callback below). Rendered as a prominent amber notice box.
    if sample_data:
        _sample_style = ParagraphStyle(
            "sample_callout", parent=s["body"],
            textColor=colors.HexColor("#7c2d12"), fontName="Helvetica-Bold", fontSize=10,
        )
        _sc = Table(
            [[Paragraph(
                "SAMPLE DATA — illustrative only. Your real watchlist is empty, so the "
                "suppliers, scores and risk signals below are a fictional example of what "
                "this report shows once you add suppliers. They are not real companies and "
                "not a statement about anyone's compliance.", _sample_style)]],
            colWidths=[6.4 * inch],
        )
        _sc.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef3c7")),
            ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#d97706")),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(_sc)
        story.append(Spacer(1, 14))

    # ── 1. Watchlist health headline ───────────────────────────────────────────
    summary = data.get("watchlist_summary") or {}
    suppliers = data.get("watched_suppliers") or []
    cards = [[
        [Paragraph(_num(summary.get("total", len(suppliers))), s["metric"]), Paragraph("SUPPLIERS WATCHED", s["metric_lbl"])],
        [Paragraph(_num(summary.get("alerting", 0)), s["metric"]), Paragraph("NEED ATTENTION", s["metric_lbl"])],
        [Paragraph(_num(summary.get("slipped", 0)), s["metric"]), Paragraph("SCORE SLIPPED (30D)", s["metric_lbl"])],
    ]]
    ct = Table(cards, colWidths=[2.2 * inch, 2.2 * inch, 2.0 * inch])
    ct.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), _PAPER),
        ("BOX", (0, 0), (-1, -1), 0.5, _RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(ct)
    story.append(Spacer(1, 10))

    alerting_names = summary.get("alerting_names") or []
    if alerting_names:
        story.append(Paragraph(
            "Suppliers needing attention this cycle: <b>"
            + _xml_escape(", ".join(str(n) for n in alerting_names)) + "</b>.",
            s["body"]))

    # ── 2. Watched-supplier roster ─────────────────────────────────────────────
    story.append(Paragraph("Your watched suppliers", s["h2"]))
    if suppliers:
        rows = [["Supplier", "Status", "Trust", "Δ", "PDPA", "Δ"]]
        for sup in suppliers[:25]:
            if sup.get("resolved"):
                status = sup.get("risk_signal") or sup.get("procurement_readiness") or "MONITORED"
            else:
                status = "UNRATED"
            rows.append([
                Paragraph(_xml_escape((sup.get("vendor_name") or "")[:38]), s["cell"]),
                Paragraph(_xml_escape(str(status)), s["cell"]),
                Paragraph(_num(sup.get("trust_score")), s["cell"]),
                _delta_cell(sup.get("trust_delta"), s),
                Paragraph(_num(sup.get("compliance_score")), s["cell"]),
                _delta_cell(sup.get("compliance_delta"), s),
            ])
        story.append(_table(rows, [2.3 * inch, 1.2 * inch, 0.7 * inch, 0.5 * inch, 0.7 * inch, 0.5 * inch]))
        story.append(Spacer(1, 4))
        story.append(Paragraph("UNRATED suppliers aren't yet a claimed profile on the platform — scores "
                               "populate once they verify. Δ is vs their previous scan.", s["small"]))
    else:
        story.append(Paragraph("You aren't watching any suppliers yet. Add suppliers to your watchlist "
                               "to start monthly monitoring of their Trust &amp; PDPA scores and risk "
                               "signals.", s["body"]))

    # ── 3. New tenders to evaluate ─────────────────────────────────────────────
    matches = data.get("tender_matches") or []
    story.append(Paragraph("New tenders to evaluate", s["h2"]))
    if matches:
        rows = [["Tender", "Agency", "Closes", "Signal"]]
        for m in matches[:10]:
            cd = m.get("closing_date")
            close = cd.strftime("%d %b %Y") if hasattr(cd, "strftime") else (str(cd) if cd else "—")
            rows.append([
                Paragraph(_xml_escape((m.get("title") or "")[:60]), s["cell"]),
                Paragraph(_xml_escape((m.get("agency") or "")[:24]), s["cell"]),
                Paragraph(close, s["cell"]),
                Paragraph(_xml_escape(m.get("bid_label") or "—"), s["cell"]),
            ])
        story.append(_table(rows, [3.0 * inch, 1.5 * inch, 1.0 * inch, 0.8 * inch]))
    else:
        story.append(Paragraph("No open tenders to surface this cycle. New GeBIZ tenders appear here as "
                               "they're published.", s["body"]))

    # ── 4. What your plan includes ─────────────────────────────────────────────
    story.append(Paragraph(f"What your {_xml_escape(plan_label)} plan includes", s["h2"]))
    for line in _TIER_INCLUDES.get(tier, _TIER_INCLUDES["pro"]):
        story.append(Paragraph(f"✓ {_xml_escape(line)}", s["body"]))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"Generated by {COMPANY_NAME} for informational purposes only. Supplier scores and risk signals "
        "are data-driven estimates, not guarantees, and not a statement of any supplier's regulatory "
        "compliance.", s["small"]))

    if sample_data:
        def _on_page(canvas, doc_):
            draw_logo_header(canvas, doc_)
            # Persistent SAMPLE-DATA strip near the foot of every page so the
            # warning survives even if a single page is printed or forwarded.
            try:
                page_w, _ = getattr(doc_, "pagesize", None) or A4
                strip_h = 0.24 * inch
                y = 0.42 * inch
                canvas.saveState()
                canvas.setFillColor(colors.HexColor("#d97706"))
                canvas.rect(0, y, page_w, strip_h, fill=1, stroke=0)
                canvas.setFillColor(colors.white)
                canvas.setFont("Helvetica-Bold", 8)
                canvas.drawCentredString(
                    page_w / 2.0, y + strip_h / 2 - 3,
                    "SAMPLE DATA — illustrative only, not your real watchlist",
                )
                canvas.restoreState()
            except Exception:
                pass
        on_first = on_later = _on_page
    else:
        on_first = on_later = draw_logo_header

    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    return buf.getvalue()


def build_buyer_procurement_report_pdf(
    db,
    user_id: str,
    *,
    tier: str = "pro",
    company: str | None = None,
    plan_label: str = "Buyer",
    demo: bool = False,
    generated_at: str | None = None,
) -> bytes:
    """Assemble report data from the DB and render the consolidated PDF.

    Single source of truth shared by the monthly digest and (future) on-demand
    download so the two never diverge. Always renders — an empty watchlist yields
    the tender-led fallback layout.

    `demo=True` (Stripe test-checkout only) substitutes a sample supplier estate
    when the buyer's real watchlist is empty, so the comparison table renders with
    believable data instead of the empty-state fallback.
    """
    from app.core.models import User
    from app.services.buyer_procurement_insights import (
        get_watched_suppliers_with_status, summarise_watchlist,
    )
    from app.services.vendor_active_insights import get_tender_matches

    user = db.query(User).filter(User.id == user_id).first()
    if not company:
        company = (getattr(user, "company", None) or "Your Organisation")

    suppliers = get_watched_suppliers_with_status(db, user_id)
    # `sample_data` drives the unmissable SAMPLE-DATA banner on every PDF page.
    # It is set ONLY when we actually substitute the fictional demo estate for an
    # empty real watchlist — a real (even if demo-checkout) populated watchlist
    # renders as itself with no banner.
    sample_data = False
    if demo and not suppliers:
        from app.services.buyer_demo_samples import demo_watched_suppliers
        suppliers = demo_watched_suppliers()
        sample_data = True

    return generate_buyer_procurement_report_pdf({
        "company_name": company,
        "plan_label": plan_label,
        "tier": tier,
        # Threaded from the digest task so the PDF's "As of" date matches the
        # email's — a single window/date for the whole deliverable.
        "generated_at": generated_at,
        "watchlist_summary": summarise_watchlist(suppliers),
        "watched_suppliers": suppliers,
        # Buyers have no VendorSector, so matches come back unclassified — still a
        # useful "new tenders to evaluate" list. Win-probability is vendor-only.
        "tender_matches": get_tender_matches(db, user_id, limit=10),
        "sample_data": sample_data,
    })
