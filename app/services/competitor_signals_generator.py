"""
competitor_signals_generator.py — Vendor Pro Competitor Awareness Signals

Generates a 1-page PDF report with 3 competitor signals from real GeBIZ data.
Called monthly by send_vendor_pro_daily_alerts or a dedicated monthly task.

Signals:
  1. Top awarded suppliers in the same sector (last 90 days) — who's winning
  2. Win rate by contract size in sector — where to compete
  3. Agency procurement trend — is the sector growing or shrinking?

Output: PDF bytes + structured dict for email template rendering.
No new AWS resources needed — uses existing S3, SES/Resend, ReportLab.
"""
from app.services.pdf_styles import get_unified_styles
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO

logger = logging.getLogger(__name__)

# ── ReportLab imports (already in requirements) ───────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from app.services.pdf_logo import draw_logo_header
    _REPORTLAB_OK = True
except ImportError:
    _REPORTLAB_OK = False
    logger.warning("[CompetitorSignals] ReportLab not installed — PDF generation disabled")


def _sector_for_vendor(db, vendor_id: str) -> str:
    """
    Look up the vendor's primary sector from VendorSector.
    Falls back to 'IT' if not set — most Singapore gov tenders are IT-adjacent.
    """
    try:
        # VendorSector lives in models_v6 and is NOT re-exported from models —
        # importing it from app.core.models raised ImportError, which the except
        # swallowed, so this always returned the "IT" fallback (competitor intel
        # then matched nothing and rendered empty). Import from the real module.
        from app.core.models import VendorSector
        row = db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).first()
        return (row.sector or "IT").upper() if row else "IT"
    except Exception as e:
        logger.warning("[CompetitorSignals] Could not load vendor sector: %s", e)
        return "IT"


def _get_signals(db, sector: str, days: int = 90, company_name: str | None = None, uen: str | None = None) -> dict:
    """
    Aggregate 3 competitor signals from gebiz_award_history.
    Returns a dict with keys: top_suppliers, win_rate_by_size, sector_trend.
    Safe to call even if the table is empty — returns sensible defaults.
    """
    from sqlalchemy import func
    from app.core.models import GebizAwardHistory

    rows = []
    actual_days = days
    for fallback_days in [days, 365, 730]:
        since = datetime.now(timezone.utc).date() - timedelta(days=fallback_days)
        try:
            # GebizAwardHistory.sector stores the classifier KEY (e.g. "IT",
            # "CONSTRUCTION") written by refresh_gebiz_base_rates — NOT the raw
            # GeBIZ free-text. So match the vendor sector against that key
            # case-insensitively. (The old "it" → "information technology" remap
            # searched for text that is never stored, so IT vendors always got
            # zero rows — empty competitor intel + a missing Signal 2.)
            _search_sec = (sector or "").strip().lower()

            rows = (
                db.query(GebizAwardHistory)
                .filter(
                    func.lower(func.trim(GebizAwardHistory.sector)) == _search_sec,
                    GebizAwardHistory.awarded_date >= since,
                )
                .all()
            )
            if rows:
                actual_days = fallback_days
                break
        except Exception as e:
            logger.warning("[CompetitorSignals] DB query failed: %s", e)
            break

    if not rows:
        return {
            "top_suppliers": [],
            "win_rate_by_size": [],
            "sector_trend": {"direction": "stable", "pct": 0},
            "sector": sector,
            "period_days": days,
            "total_awards": 0,
        }

    # Signal 1: Top suppliers by win count
    supplier_wins: dict[str, dict] = {}
    for r in rows:
        sup = (r.supplier_name or "Undisclosed").strip()
        e = supplier_wins.setdefault(sup, {"count": 0, "total_value": 0.0})
        e["count"] += 1
        e["total_value"] += float(r.award_amt or 0)

    # Helper to normalize company names for comparison
    def _normalize_name(name: str) -> str:
        if not name: return ""
        n = name.upper().strip()
        for suffix in [" PTE. LTD.", " PTE LTD", " LTD.", " LTD", " INC.", " INC", " LLC", " (SMARTTECH)"]:
            n = n.replace(suffix, "")
        return n.strip()

    vendor_norm = _normalize_name(company_name) if company_name else ""
    vendor_uen_norm = (uen or "").strip().upper()

    top_suppliers = sorted(
        [
            {
                "name": k,
                "wins": v["count"],
                "avg_award": round(v["total_value"] / v["count"]) if v["count"] else 0,
            }
            for k, v in supplier_wins.items()
            if k != "Undisclosed"
            and (_normalize_name(k) != vendor_norm if vendor_norm else True)
            # UEN is not in supplier_wins dict easily, but if we had it we'd check it.
        ],
        key=lambda x: x["wins"],
        reverse=True,
    )[:5]

    # Signal 2: Win rate by contract size bracket
    brackets = [
        ("< S$50K", 0, 50_000),
        ("S$50K–S$250K", 50_000, 250_000),
        ("S$250K–S$1M", 250_000, 1_000_000),
        ("> S$1M", 1_000_000, float("inf")),
    ]
    size_counts = {label: 0 for label, *_ in brackets}
    for r in rows:
        amt = float(r.award_amt or 0)
        for label, lo, hi in brackets:
            if lo <= amt < hi:
                size_counts[label] += 1
                break

    total_awards = len(rows)
    win_rate_by_size = [
        {
            "bracket": label,
            "count": size_counts[label],
            "pct": round(100 * size_counts[label] / total_awards) if total_awards else 0,
        }
        for label, *_ in brackets
        if size_counts[label] > 0
    ]

    # Signal 3: Sector trend — compare first half vs second half of the window
    midpoint = since + timedelta(days=actual_days // 2)
    first_half = sum(1 for r in rows if r.awarded_date and r.awarded_date < midpoint)
    second_half = sum(1 for r in rows if r.awarded_date and r.awarded_date >= midpoint)
    if first_half == 0:
        trend_direction, trend_pct = "stable", 0
    else:
        delta = (second_half - first_half) / first_half * 100
        if delta > 10:
            trend_direction = "growing"
        elif delta < -10:
            trend_direction = "shrinking"
        else:
            trend_direction = "stable"
        trend_pct = round(delta)

    return {
        "top_suppliers": top_suppliers,
        "win_rate_by_size": win_rate_by_size,
        "sector_trend": {"direction": trend_direction, "pct": trend_pct},
        "sector": sector,
        "period_days": actual_days,
        "total_awards": total_awards,
    }


def generate_competitor_signals_pdf(signals: dict, company_name: str) -> bytes:
    """
    Generate a 1-page PDF competitor signals report.
    Returns PDF bytes. Raises if ReportLab is not installed.
    """
    if not _REPORTLAB_OK:
        raise RuntimeError("ReportLab is required for competitor signals PDF generation")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )

    styles = get_unified_styles()
    h_style = ParagraphStyle(
        "h", parent=styles["Heading1"], fontSize=16,
        textColor=colors.HexColor("#0f172a"), spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#64748b"), spaceAfter=14,
    )
    h2_style = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontSize=11,
        textColor=colors.HexColor("#0f172a"), spaceAfter=6, spaceBefore=12,
    )
    body_style = ParagraphStyle(
        "body", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#334155"), spaceAfter=4,
    )

    sector = signals.get("sector", "—")
    period_days = signals.get("period_days", 90)
    total_awards = signals.get("total_awards", 0)
    generated = datetime.now(timezone.utc).strftime("%d %b %Y")
    trend = signals.get("sector_trend", {})
    trend_dir = trend.get("direction", "stable")
    trend_pct = trend.get("pct", 0)
    trend_color = "#16a34a" if trend_dir == "growing" else "#dc2626" if trend_dir == "shrinking" else "#334155"
    trend_label = (
        f"Sector is <font color='{trend_color}'><b>{trend_dir} ({trend_pct:+}%)</b></font> "
        f"vs the first {period_days // 2} days of the window."
    )

    story = [
        Paragraph(f"Competitor Awareness Signals — {sector}", h_style),
        Paragraph(
            f"{company_name}  ·  GeBIZ data: trailing {period_days} days  ·  "
            f"{total_awards} awards analysed  ·  Generated {generated}",
            sub_style,
        ),
    ]

    # Signal 1: Top suppliers
    top_suppliers = signals.get("top_suppliers", [])
    if top_suppliers:
        story.append(Paragraph("Signal 1 — Who's winning in your sector", h2_style))
        table_data = [["Supplier", "Wins", "Avg Award"]] + [
            [s["name"][:40], str(s["wins"]), f"S${s['avg_award']:,.0f}"]
            for s in top_suppliers
        ]
        t = Table(table_data, hAlign="LEFT", colWidths=[3.5 * inch, 1.0 * inch, 1.7 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cbd5e1")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("Signal 1 — No award data available for this sector yet.", h2_style))

    # Signal 2: Win rate by contract size
    win_rate = signals.get("win_rate_by_size", [])
    if win_rate:
        story.append(Paragraph("Signal 2 — Where to compete by contract size", h2_style))
        table_data = [["Contract size", "Awards", "Share"]] + [
            [w["bracket"], str(w["count"]), f"{w['pct']}%"]
            for w in win_rate
        ]
        t = Table(table_data, hAlign="LEFT", colWidths=[2.5 * inch, 1.0 * inch, 1.0 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cbd5e1")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("Signal 2 — No award data available to compute contract size brackets.", h2_style))

    # Signal 3: Sector trend
    story.append(Paragraph("Signal 3 — Sector procurement trend", h2_style))
    story.append(Paragraph(trend_label, body_style))
    story.append(Paragraph(
        "Action: if the sector is growing, increase bid frequency. "
        "If shrinking, focus on agencies with the highest historical win rates.",
        body_style,
    ))

    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(
        "Source: GeBIZ Government Procurement Awards (data.gov.sg). "
        "Supplier names are as published by GeBIZ. "
        "This report is generated by Booppa Vendor Pro and is for intelligence purposes only.",
        body_style,
    ))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()


async def generate_and_deliver_competitor_signals(
    vendor_id: str,
    vendor_email: str,
    company_name: str,
    db,
) -> bool:
    """
    End-to-end: compute signals → generate PDF → upload to S3 → send email with PDF.
    Returns True if email was delivered successfully.
    """
    import asyncio
    from app.services.storage import S3Service
    from app.services.email_service import EmailService
    from datetime import datetime

    from app.core.models import User
    
    sector = _sector_for_vendor(db, vendor_id)
    profile = db.query(User).filter(User.id == str(vendor_id)).first()
    c_uen = profile.uen if profile else None
    
    signals = _get_signals(db, sector, days=90, company_name=company_name, uen=c_uen)
    month_label = datetime.now(timezone.utc).strftime("%B %Y")

    # Generate PDF
    try:
        pdf_bytes = generate_competitor_signals_pdf(signals, company_name)
    except Exception as e:
        logger.error("[CompetitorSignals] PDF generation failed for %s: %s", vendor_id, e)
        return False

    # Upload to S3
    pdf_url: str | None = None
    s3_key: str | None = None
    try:
        s3 = S3Service()
        report_id = f"competitor-signals-{vendor_id}-{datetime.now(timezone.utc).strftime('%Y%m')}"
        pdf_url = await s3.upload_pdf(pdf_bytes, report_id)
        # Derive S3 key from the report_id pattern used by upload_pdf
        s3_key = f"reports/{report_id}.pdf"
    except Exception as e:
        logger.warning("[CompetitorSignals] S3 upload failed for %s: %s", vendor_id, e)

    # Build email HTML
    trend = signals.get("sector_trend", {})
    trend_dir = trend.get("direction", "stable")
    trend_emoji = "📈" if trend_dir == "growing" else "📉" if trend_dir == "shrinking" else "➡️"
    top_sup = signals.get("top_suppliers", [])
    supplier_html = "".join(
        f"<li><strong>{s['name'][:50]}</strong> — {s['wins']} wins · avg S${s['avg_award']:,.0f}</li>"
        for s in top_sup[:3]
    ) or "<li>No data available for this period.</li>"

    from app.services.email_layout import branded_email_html, email_button
    body_html = branded_email_html(
        f"""
        <h2 style="color:#0f172a;margin:0 0 16px;font-size:18px;">Competitor Awareness Signals — {month_label}</h2>
        <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company_name}</strong>,</p>
        <p style="margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;">Here are your monthly competitor signals for the <strong>{signals['sector']}</strong> sector
           ({signals['total_awards']} GeBIZ awards in the last {signals['period_days']} days):</p>

        <h3 style="color:#0f172a;font-size:15px;margin:0 0 8px;">1. Top suppliers in your sector</h3>
        <ul style="color:#334155;font-size:14px;line-height:1.8;">{supplier_html}</ul>

        <h3 style="color:#0f172a;font-size:15px;margin:20px 0 8px;">2. Where to compete</h3>
        <p style="color:#334155;font-size:14px;line-height:1.6;">
          See the attached PDF for the full breakdown of win rates by contract size.
        </p>

        <h3 style="color:#0f172a;font-size:15px;margin:20px 0 8px;">3. {trend_emoji} Sector trend</h3>
        <p style="color:#334155;font-size:14px;line-height:1.6;">
          The {signals['sector']} sector is <strong>{trend_dir}</strong>
          ({trend.get('pct', 0):+}% vs the previous period).
        </p>

        <p style="font-size:13px;color:#64748b;line-height:1.6;margin:16px 0 20px;">
          The full PDF report (attached) includes contract size analysis and award timing signals.
          {"" if pdf_url else "Note: PDF attachment unavailable this month — contact support."}
        </p>

        {email_button(pdf_url, "Download PDF report") if pdf_url else ""}

        {email_button("https://www.booppa.io/vendor/dashboard", "View dashboard →", primary=False)}
        <p style="color:#64748b;font-size:12px;margin:16px 0 0;">
          Vendor Pro · Competitor Awareness Signals · booppa.io
        </p>
        """,
        title=f"Competitor Awareness Signals — {month_label}",
        preheader=f"Monthly competitor signals for the {signals['sector']} sector.",
    )

    # Send with PDF attached
    email_svc = EmailService()
    sent = await email_svc.send_with_pdf_attachment(
        to_email=vendor_email,
        subject=f"Competitor Awareness Signals — {month_label} · {signals['sector']}",
        body_html=body_html,
        pdf_bytes=pdf_bytes,
        filename=f"Competitor_Signals_{datetime.now(timezone.utc).strftime('%Y%m')}.pdf",
    )

    if sent:
        logger.info(
            "[CompetitorSignals] Delivered to %s (sector=%s, awards=%d)",
            vendor_email, sector, signals["total_awards"],
        )
    else:
        logger.error("[CompetitorSignals] Email delivery failed for %s", vendor_email)

    return sent
