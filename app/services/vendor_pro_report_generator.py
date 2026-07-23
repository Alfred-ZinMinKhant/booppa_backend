"""Vendor Pro Monthly Intelligence Report — the flagship SGD 99/mo deliverable.

A multi-section PDF that consolidates everything Vendor Pro pays for into one
board-presentable artifact:

  1. Scores + trend + sector benchmark
  2. Win-probability tender pipeline (personalised BID/WATCH/PASS + win %)
  3. Competitor intelligence (top suppliers, win-rate by value band, sector trend)
  4. PDPA posture + drift
  5. What your plan includes

Every section degrades gracefully when its data is missing — the report always
renders. Reuses the shared logo header (`pdf_logo.draw_logo_header`) and the same
styling vocabulary as `vendor_snapshot_generator`.
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

from app.services.pdf_styles import get_unified_styles
from app.services.pdf_logo import draw_logo_header
from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

VENDOR_PRO_REPORT_SCHEMA_VERSION = 1

_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")
_PAPER = colors.HexColor("#f8fafc")


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


def generate_vendor_pro_report_pdf(data: Dict[str, Any]) -> bytes:
    """Render the consolidated report. `data` keys (all optional except company_name):
      company_name, plan_label, generated_at,
      trust_score, compliance_score, profile_views_30d,
      trend: {total_delta, compliance_delta, sector_percentile},
      sector_benchmark: {sector, percentile},
      tender_matches: [{title, agency, closing_date, bid_label, win_probability}],
      competitor_pulse: {top_suppliers, win_rate_by_size, sector_trend, sector, total_awards},
      pdpa_drift: {current_score, previous_score, dimension_changes},
    """
    s = get_unified_styles()
    company = data.get("company_name") or "Your Company"
    plan_label = data.get("plan_label") or "Vendor Pro"
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    def _num(v):
        return "—" if v is None else str(v)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"Vendor Pro Monthly Intelligence Report — {company}",
    )
    story: list = []

    story.append(Paragraph("Vendor Pro Monthly Intelligence Report", s["title"]))
    story.append(Paragraph(_xml_escape(company), s["sub"]))
    story.append(Paragraph(f"{_xml_escape(plan_label)} &middot; As of {gen_at} &middot; {COMPANY_NAME}", s["small"]))
    story.append(Spacer(1, 16))

    # ── 1. Scores + trend + benchmark ──────────────────────────────────────────
    cards = [[
        [Paragraph(_num(data.get("trust_score")), s["metric"]), Paragraph("TRUST SCORE / 100", s["metric_lbl"])],
        [Paragraph(_num(data.get("compliance_score")), s["metric"]), Paragraph("COMPLIANCE / 100", s["metric_lbl"])],
        [Paragraph(_num(data.get("profile_views_30d")), s["metric"]), Paragraph("PROFILE VIEWS (30D)", s["metric_lbl"])],
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

    trend = data.get("trend") or {}
    bench = data.get("sector_benchmark") or {}

    def _delta_txt(d, label):
        if d is None:
            return None
        if d > 0:
            return f'<font color="#16a34a">▲ {d}</font> {label}'
        if d < 0:
            return f'<font color="#dc2626">▼ {abs(d)}</font> {label}'
        return f'{label}: no change'

    bits = [t for t in (_delta_txt(trend.get("total_delta"), "Trust"),
                        _delta_txt(trend.get("compliance_delta"), "Compliance")) if t]
    if bench.get("percentile") is not None and bench.get("sector"):
        pct = int(bench["percentile"])
        bits.append(f'Sector standing: <b>top {max(1, 100 - pct)}%</b> in {_xml_escape(bench["sector"])} '
                    f'(ahead of {pct}% of peers)')
    if bits:
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(bits) + " — vs last cycle.", s["body"]))

    # ── 2. Win-probability tender pipeline ─────────────────────────────────────
    matches = data.get("tender_matches") or []
    story.append(Paragraph("Tender pipeline — where to focus", s["h2"]))
    if matches:
        rows = [["Tender", "Agency", "Closes", "Signal", "Win %"]]
        for m in matches[:10]:
            cd = m.get("closing_date")
            close = cd.strftime("%d %b %Y") if hasattr(cd, "strftime") else (str(cd) if cd else "—")
            wp = m.get("win_probability")
            rows.append([
                Paragraph(_xml_escape((m.get("title") or "")[:60]), s["cell"]),
                Paragraph(_xml_escape((m.get("agency") or "")[:24]), s["cell"]),
                Paragraph(close, s["cell"]),
                Paragraph(_xml_escape(m.get("bid_label") or "—"), s["cell"]),
                Paragraph(f"{wp}%" if wp is not None else "—", s["cell"]),
            ])
        story.append(_table(rows, [2.5 * inch, 1.3 * inch, 0.9 * inch, 0.8 * inch, 0.7 * inch]))
        story.append(Spacer(1, 4))
        story.append(Paragraph("Win % is an estimate from your verification depth, evidence, and "
                               "sector standing. Guidance, not a guarantee.", s["small"]))
    else:
        story.append(Paragraph("No open tenders matched your sector this cycle. Set or refine your "
                               "sector tag to receive matches.", s["body"]))

    # ── 3. Competitor intelligence ─────────────────────────────────────────────
    pulse = data.get("competitor_pulse") or {}
    story.append(Paragraph("Competitor intelligence", s["h2"]))
    top = pulse.get("top_suppliers") or []
    if top:
        rows = [["Top suppliers (your sector)", "Wins", "Avg award"]]
        for sup in top[:5]:
            name = sup.get("name") or sup.get("supplier") or "—"
            wins = sup.get("count") or sup.get("wins") or "—"
            avg = sup.get("avg_value") or sup.get("total_value")
            avg_txt = f"S${int(avg):,}" if isinstance(avg, (int, float)) and avg else "—"
            rows.append([Paragraph(_xml_escape(str(name)), s["cell"]),
                         Paragraph(str(wins), s["cell"]),
                         Paragraph(avg_txt, s["cell"])])
        story.append(_table(rows, [3.8 * inch, 1.1 * inch, 1.5 * inch]))
        trend_d = (pulse.get("sector_trend") or {}).get("direction")
        if trend_d:
            story.append(Spacer(1, 6))
            story.append(Paragraph(
                f"Sector award activity is <b>{_xml_escape(trend_d)}</b> over the last "
                f"{pulse.get('period_days', 90)} days ({pulse.get('total_awards', 0)} awards analysed).",
                s["body"]))
    else:
        story.append(Paragraph("No recent award activity found for your sector. Competitor signals "
                               "populate as GeBIZ award data accrues.", s["body"]))

    # ── 4. PDPA posture + drift ────────────────────────────────────────────────
    drift = data.get("pdpa_drift") or {}
    story.append(Paragraph("PDPA posture & drift", s["h2"]))
    if drift.get("current_score") is not None:
        cur, prev = drift.get("current_score"), drift.get("previous_score")
        delta_txt = ""
        if prev is not None:
            d = cur - prev
            delta_txt = (f' (<font color="#16a34a">▲ {d}</font> vs last scan)' if d > 0
                         else f' (<font color="#dc2626">▼ {abs(d)}</font> vs last scan)' if d < 0
                         else " (no change vs last scan)")
        story.append(Paragraph(f"Current PDPA compliance score: <b>{cur}/100</b>{delta_txt}.", s["body"]))
        changes = drift.get("dimension_changes") or []
        if changes:
            rows = [["Dimension", "Was", "Now"]]
            for c in changes[:8]:
                rows.append([
                    Paragraph(_xml_escape(c.get("dimension_name", "")), s["cell"]),
                    Paragraph(_xml_escape(c.get("previous_status", "")), s["cell"]),
                    Paragraph(_xml_escape(c.get("current_status", "")), s["cell"]),
                ])
            story.append(Spacer(1, 6))
            story.append(Paragraph("Dimensions that slipped since last scan:", s["body"]))
            story.append(_table(rows, [3.4 * inch, 1.5 * inch, 1.5 * inch]))
        else:
            story.append(Paragraph("No dimensions worsened since your last scan.", s["small"]))
    else:
        story.append(Paragraph("No PDPA scan on file yet. Run a PDPA scan to start drift tracking.", s["body"]))

    # ── 5. Score basis — what drove each dimension ─────────────────────────────
    # A score with no stated basis is a number, not an argument. Every row here
    # is the signal the scan actually recorded, plus whether that basis was
    # inferred from public disclosure or backed by tested evidence.
    basis_rows = data.get("score_basis") or []
    if basis_rows:
        story.append(Paragraph("Score basis", s["h2"]))
        story.append(Paragraph(
            "Each dimension below shows the public signal that drove its score and whether "
            "the basis is an inference or evidence you have tested. Tested evidence is "
            "annotated here; it does not itself change the score.", s["body"]))
        story.append(Spacer(1, 6))
        rows = [["Dimension", "Status", "Score", "Driving signal", "Basis"]]
        for r in basis_rows:
            score = r.get("score")
            rows.append([
                Paragraph(_xml_escape(r.get("dimension_name") or ""), s["cell"]),
                Paragraph(_xml_escape(r.get("status") or "—"), s["cell"]),
                Paragraph(str(score) if score is not None else "—", s["cell"]),
                Paragraph(_xml_escape(r.get("signal") or ""), s["cell"]),
                Paragraph(_xml_escape(r.get("basis") or ""), s["cell"]),
            ])
        story.append(_table(rows, [1.5 * inch, 0.75 * inch, 0.5 * inch, 2.35 * inch, 1.3 * inch]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "\"Inferred (public scan)\" means the signal was read from publicly available "
            "disclosure, not verified against your internal controls. Upload tested evidence "
            "against the matching MAS TRM domain to change a row to \"Tested\".", s["small"]))

    # ── 6. What your plan includes ─────────────────────────────────────────────
    story.append(Paragraph("What your Vendor Pro plan includes", s["h2"]))
    for line in [
        "This consolidated monthly intelligence report",
        "Win-probability tender pipeline with BID/WATCH/PASS signals",
        "Sector competitor intelligence",
        "Quarterly PDPA Snapshot with drift tracking",
        "1 notarization per month",
        "Priority placement + Active badge on your public profile",
    ]:
        story.append(Paragraph(f"✓ {line}", s["body"]))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"Generated by {COMPANY_NAME} for informational purposes only. Tender win probabilities and "
        "competitor signals are data-driven estimates, not guarantees. Not a statement of regulatory "
        "compliance.", s["small"]))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()


def build_pro_report_pdf(
    db,
    vendor_id: str,
    *,
    company: str | None = None,
    plan_label: str = "Vendor Pro",
    trust_score=None,
    compliance_score=None,
    profile_views_30d=None,
) -> bytes:
    """Assemble report data from the DB and render the consolidated PDF.

    Single source of truth shared by the monthly digest and the on-demand
    download endpoint so the two never diverge. Score/views may be passed in
    (the digest already computes them) or are read here when omitted.
    """
    from app.core.models import User, VendorScore, VerifyRecord, ProofView
    from app.services.vendor_active_insights import (
        get_score_trend, get_sector_benchmark, get_tender_matches,
        get_competitor_pulse, get_pdpa_drift,
    )
    from app.services.score_basis import build_score_basis

    user = db.query(User).filter(User.id == vendor_id).first()
    if not company:
        from app.services.evidence_enricher import display_legal_name
        company = display_legal_name(user, db) or "Your Company"

    if trust_score is None or compliance_score is None:
        sr = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        if trust_score is None:
            trust_score = getattr(sr, "total_score", None)
        if compliance_score is None:
            compliance_score = getattr(sr, "compliance_score", None)

    if profile_views_30d is None:
        try:
            from datetime import timedelta
            verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
            if verify:
                since = datetime.now(timezone.utc) - timedelta(days=30)
                profile_views_30d = (
                    db.query(ProofView)
                    .filter(ProofView.verify_id == verify.id, ProofView.created_at >= since)
                    .count()
                )
        except Exception:
            profile_views_30d = None

    return generate_vendor_pro_report_pdf({
        "company_name": company,
        "plan_label": plan_label,
        "trust_score": trust_score,
        "compliance_score": compliance_score,
        "profile_views_30d": profile_views_30d,
        "trend": get_score_trend(db, vendor_id),
        "sector_benchmark": get_sector_benchmark(db, vendor_id),
        "tender_matches": get_tender_matches(db, vendor_id, limit=10, with_win_probability=True),
        "competitor_pulse": get_competitor_pulse(db, vendor_id),
        "pdpa_drift": get_pdpa_drift(db, vendor_id),
        "score_basis": build_score_basis(db, vendor_id),
    })
