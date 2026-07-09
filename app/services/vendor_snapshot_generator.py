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

from app.services.pdf_styles import get_unified_styles
from app.services.pdf_logo import draw_logo_header

from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

VENDOR_SNAPSHOT_SCHEMA_VERSION = 2


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _verification_label(value: Any) -> str:
    """Human-readable verification level.

    The source may pass a ``VerificationLevel`` enum (whose ``str()`` is the
    code artifact ``VerificationLevel.PREMIUM``), the raw value ``"PREMIUM"``,
    or ``None``. Normalise all of these to a title-cased label, e.g. ``Premium``.
    """
    if value is None:
        return "Standard"
    raw = getattr(value, "value", value)  # enum -> "PREMIUM"; str -> str
    return str(raw).replace("_", " ").strip().title() or "Standard"




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
      sector_rank:  {sector, rank, total} (optional)
      search_impressions_30d: int|None — buyer-search appearances, trailing 30d
      tender_matches: list of {title, closing_date, bid_label} (optional)
    """
    s = get_unified_styles("vs_")
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
        _n = bench.get("peer_count", 0)
        _sector = _xml_escape(bench["sector"])
        if bench.get("basis") == "sector" and _n:
            trend_bits.append(
                f'Sector standing: at or above <b>{pct}%</b> of {_n} scored peers in {_sector}'
            )
        else:
            trend_bits.append(
                f'Sector standing: <b>top {max(1, 100 - pct)}%</b> in {_sector} '
                f'(ahead of {pct}% of peers)'
            )
    if trend_bits:
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(trend_bits), s["body"]))
        story.append(Spacer(1, 14))

    # Trust Score breakdown — per-dimension scores with the action that lifts
    # each and the points it would add (4b). Turns a single number into a plan.
    breakdown = data.get("trust_breakdown") or {}
    bd_dims = breakdown.get("dimensions") or []
    if bd_dims:
        story.append(Paragraph("Trust Score Breakdown", s["h2"]))
        bd_rows = [["Dimension", "Score", "Action to improve"]]
        for d in bd_dims:
            pts = int(d.get("potential_points") or 0)
            action = _xml_escape(d.get("action") or "")
            if pts > 0:
                action = f'{action} <font color="#16a34a">(+{pts} pts)</font>'
            else:
                action = '<font color="#16a34a">Fully scored ✓</font>'
            bd_rows.append([
                Paragraph(_xml_escape(d.get("label") or ""), s["body"]),
                Paragraph(f'{int(d.get("score") or 0)}/100', s["body"]),
                Paragraph(action, s["body"]),
            ])
        bd_tbl = Table(bd_rows, colWidths=[1.4 * inch, 0.9 * inch, 4.1 * inch])
        bd_tbl.setStyle(TableStyle([
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
        story.append(bd_tbl)
        story.append(Spacer(1, 8))
        top_actions = breakdown.get("top_actions") or []
        projected = breakdown.get("projected_total")
        if top_actions and projected is not None:
            story.append(Paragraph(
                f"With these {len(top_actions)} action"
                f"{'s' if len(top_actions) != 1 else ''}, your Trust Score reaches "
                f"<b>{int(projected)}/100</b>.", s["body"]))
            story.append(Spacer(1, 14))

    # Visibility & ranking — absolute rank among sector peers (4e) plus the
    # real "appeared in N searches this month" count from the search-impression
    # log. When no impressions have been recorded yet we fall back to an honest
    # status note rather than a fabricated number.
    rank = data.get("sector_rank") or {}
    rank_bits: List[str] = []
    if rank.get("rank") and rank.get("total") and rank.get("sector"):
        rank_bits.append(
            f'Your position in <b>{_xml_escape(rank["sector"])}</b> vendor searches: '
            f'<b>#{int(rank["rank"])}</b> of {int(rank["total"])} active vendors'
        )
    impressions = data.get("search_impressions_30d")
    if isinstance(impressions, int) and impressions > 0:
        rank_bits.append(
            f'Your profile appeared in <b>{impressions}</b> buyer '
            f'{"search" if impressions == 1 else "searches"} this month.'
        )
    elif not (data.get("profile_views_30d") or 0):
        rank_bits.append(
            "Your profile is live and prioritised — search appearances and views "
            "accumulate as buyer traffic grows on the platform. "
            "<i>(Month 1: Building baseline visibility)</i>"
        )
    bd_top = (breakdown.get("top_actions") or [])
    if bd_top:
        rank_bits.append(
            f'Highest-impact action this month: {_xml_escape(bd_top[0].get("action") or "")}.'
        )
    if rank_bits:
        story.append(Paragraph("Visibility &amp; Ranking", s["h2"]))
        for b in rank_bits:
            story.append(Paragraph(b, s["body"]))
            story.append(Spacer(1, 4))
        story.append(Spacer(1, 10))

    # Personalised tender matches (BID/WATCH/PASS).
    matches = data.get("tender_matches") or []
    if matches:
        story.append(Paragraph("Tender matches — should you bid?", s["h2"]))
        # Show the win-probability column only when at least one match carries a
        # value (Vendor Pro / Active with_win_probability); otherwise omit it so
        # Active-without-scoring isn't a column of dashes.
        show_wp = any(m.get("win_probability") is not None for m in matches[:5])
        header = ["Tender", "Closes", "Signal"] + (["Win %"] if show_wp else [])
        m_rows = [header]
        for m in matches[:5]:
            cd = m.get("closing_date")
            close = cd.strftime("%d %b %Y") if hasattr(cd, "strftime") else (str(cd) if cd else "—")
            row = [
                Paragraph(_xml_escape((m.get("title") or "")[:80]), s["body"]),
                Paragraph(close, s["body"]),
                Paragraph(_xml_escape(m.get("bid_label") or "—"), s["body"]),
            ]
            if show_wp:
                wp = m.get("win_probability")
                wp_txt = f"{wp:.0f}%" if isinstance(wp, (int, float)) else "—"
                row.append(Paragraph(wp_txt, s["body"]))
            m_rows.append(row)
        col_widths = ([3.4 * inch, 1.3 * inch, 0.9 * inch, 0.8 * inch]
                      if show_wp else [3.9 * inch, 1.4 * inch, 1.1 * inch])
        m_tbl = Table(m_rows, colWidths=col_widths)
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
    else:
        # Empty matches (new vendor, or no open tenders fit the profile this
        # cycle). Show an honest zero-state instead of dropping the whole
        # section, so the report never looks broken to a first-cycle vendor.
        story.append(Paragraph("Tender matches — should you bid?", s["h2"]))
        story.append(Paragraph(
            "No open tenders matched your sector and profile this cycle. Matches "
            "appear here as relevant GeBIZ tenders open — keep your sector and "
            "capabilities up to date on your profile to widen the match.", s["body"]))
        story.append(Spacer(1, 14))

    story.append(Paragraph("Profile Details", s["h2"]))
    rows: List[Tuple[str, str]] = [
        ("Verification Level", _verification_label(data.get("verification_level"))),
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
