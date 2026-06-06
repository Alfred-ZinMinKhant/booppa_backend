"""
Compliance Evidence Pack — Summary Cover Sheet Generator
=========================================================
9-section ReportLab PDF delivered after all bundle components are ready.

Sections:
  1. Cover / Executive Summary
  2. Bundle Components Delivered
  3. PDPA Compliance Status
  4. RFP Complete Summary
  5. Blockchain Evidence Trail (PDPA + RFP anchored, signed sheet pending)
  6. MAS TRM Assessment (if available)
  7. Risk Overview
  8. Recommendations
  9. Legal Disclaimer
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional, Dict, Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Logo resolution mirrors pdf_service.py so both generators find the same asset
# regardless of whether running from source tree or container.
_HERE = os.path.dirname(__file__)
_LOGO_CANDIDATES = [
    os.path.join(_HERE, "..", "..", "static", "logo.png"),
    "/app/static/logo.png",
    os.path.join(_HERE, "..", "..", "data", "logo.png"),
    "/app/data/logo.png",
]
_LOGO_PATH: str | None = None
for _c in _LOGO_CANDIDATES:
    _abs = os.path.abspath(_c)
    if os.path.exists(_abs):
        _LOGO_PATH = _abs
        break

# Bumped whenever the visible structure of the cover sheet changes (sections,
# branding, copy). Stored on the Report row so the UI can detect customers
# holding an older PDF and offer a free regenerate.
# v3: prominent body logo on cover page; full PDPA findings list (not top-3);
#     full RFP Q&A list embedded (not just count + summary).
# v4: RFP Q&A blocks now show the per-answer verification source (intake /
#     website / ACRA / SSL / GeBIZ / intake+website / etc.) instead of the
#     binary fact-backed/AI-generated badge; evidence line under each answer.
COVER_SHEET_SCHEMA_VERSION = 4

PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
# Header tall enough to fit the Booppa logo at a legible size on the navy band.
HEADER_H = 0.7 * inch
FOOTER_H = 0.45 * inch

NAVY    = colors.HexColor("#0f172a")
EMERALD = colors.HexColor("#10b981")
SLATE   = colors.HexColor("#64748b")
LIGHT   = colors.HexColor("#f8fafc")
BORDER  = colors.HexColor("#e2e8f0")
WHITE   = colors.white


def _draw_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    # The logo is ~423×144 px → aspect ~2.94. ReportLab's drawImage with only
    # `height` and preserveAspectRatio is unreliable on some builds (silently
    # skips). Compute the matching width explicitly so the logo always renders.
    logo_h = 0.48 * inch
    logo_w = logo_h * 2.94
    logo_y = PAGE_H - HEADER_H + (HEADER_H - logo_h) / 2
    logo_drawn = False
    if _LOGO_PATH:
        try:
            canvas.drawImage(
                _LOGO_PATH,
                MARGIN,
                logo_y,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
            logo_drawn = True
        except Exception:
            logo_drawn = False
    if not logo_drawn:
        canvas.setFillColor(EMERALD)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(MARGIN, PAGE_H - HEADER_H + 0.26 * inch, "BOOPPA")

    # Right side: pack label. Drop the centre "Compliance Evidence Pack"
    # caption — the logo + right label is enough and was overlapping the
    # logo at the old size.
    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.26 * inch, "COMPLIANCE EVIDENCE PACK · COVER SHEET")

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, "Booppa Smart Care LLC · booppa.io · Confidential")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


def _section(title: str, styles, *, page_break: bool = False) -> list:
    """Section header. Pass `page_break=True` to start the section on a fresh
    page (used between heavy variable-length sections so they don't collide).
    The HR is wrapped in a KeepTogether with the next flowable via ReportLab's
    keepWithNext on the h2 style — set globally below so the title never
    widows at the end of a page on its own.
    """
    out: list = []
    if page_break:
        out.append(PageBreak())
    else:
        out.append(Spacer(1, 0.15 * inch))
    out.append(Paragraph(f'<font color="#10b981">■</font>  <b>{title}</b>', styles["h2"]))
    out.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6))
    return out


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    def _val(v):
        return v if isinstance(v, Paragraph) else Paragraph(str(v), _STYLES["Normal"])
    data = [[Paragraph(f"<b>{k}</b>", _STYLES["Normal"]), _val(v)] for k, v in rows]
    t = Table(data, colWidths=[2.2 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT, WHITE]),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


_RAW_STYLES = getSampleStyleSheet()
_STYLES: Dict[str, ParagraphStyle] = {
    "Normal": ParagraphStyle("cs_normal", fontSize=8, leading=12, textColor=colors.HexColor("#334155")),
    "h1": ParagraphStyle("cs_h1", fontSize=18, leading=22, textColor=NAVY, fontName="Helvetica-Bold"),
    # keepWithNext=True so a section title can't widow at the bottom of a page
    # without at least one line of its content tagging along.
    "h2": ParagraphStyle("cs_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
    "small": ParagraphStyle("cs_small", fontSize=7, leading=10, textColor=SLATE),
    "caption": ParagraphStyle("cs_caption", fontSize=9, leading=13, textColor=colors.HexColor("#334155")),
}

_SEVERITY_COLORS = {
    "CRITICAL": colors.HexColor("#7f1d1d"),
    "HIGH":     colors.HexColor("#dc2626"),
    "MEDIUM":   colors.HexColor("#f59e0b"),
    "LOW":      colors.HexColor("#10b981"),
    "INFO":     SLATE,
}


def _pdpa_finding_block(idx: int, f: dict):
    """Render a single PDPA finding as a bordered card: title + severity badge,
    description, legislation, evidence, recommendation. Uses KeepTogether so
    findings don't split across pages mid-card when possible.
    """
    sev = (f.get("severity") or "MEDIUM").upper()
    sev_color = _SEVERITY_COLORS.get(sev, SLATE)
    title = _xml_escape(
        f.get("title") or (f.get("type") or f.get("check_id") or "Finding").replace("_", " ").title()
    )
    description = _xml_escape(f.get("description") or f.get("details") or "—")
    legislation = _xml_escape(
        f.get("legislation_text")
        or "; ".join(f.get("legislation_references") or [])
        or "—"
    )
    evidence = _xml_escape(f.get("evidence") or "Automated scan detection")
    recommendation = _xml_escape(f.get("recommendation") or f.get("remediation") or "—")

    header_style = ParagraphStyle(
        "find_title", fontSize=9, leading=12, textColor=NAVY, fontName="Helvetica-Bold"
    )
    sev_style = ParagraphStyle(
        "find_sev", fontSize=7, leading=9, textColor=WHITE, fontName="Helvetica-Bold",
        alignment=1, backColor=sev_color, borderPadding=2,
    )
    body_label = ParagraphStyle(
        "find_label", fontSize=7, leading=9, textColor=SLATE, fontName="Helvetica-Bold"
    )
    body_text = ParagraphStyle(
        "find_text", fontSize=8, leading=11, textColor=colors.HexColor("#334155")
    )

    header = Table(
        [[
            Paragraph(f"{idx}. {title}", header_style),
            Paragraph(sev, sev_style),
        ]],
        colWidths=[5.5 * inch, 0.8 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (1, 0), (1, 0), sev_color),
        ("TEXTCOLOR", (1, 0), (1, 0), WHITE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    rows = [
        [Paragraph("Description", body_label),    Paragraph(description, body_text)],
        [Paragraph("Legislation", body_label),    Paragraph(legislation, body_text)],
        [Paragraph("Evidence", body_label),       Paragraph(evidence, body_text)],
        [Paragraph("Recommendation", body_label), Paragraph(recommendation, body_text)],
    ]
    body = Table(rows, colWidths=[1.0 * inch, 5.3 * inch])
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBEFORE", (0, 0), (0, -1), 2, sev_color),
    ]))

    return KeepTogether([header, body])


def _xml_escape(s: str) -> str:
    """Escape user-supplied text so ReportLab's Paragraph mini-XML doesn't
    misinterpret `&`, `<`, `>` (e.g. "Q&A" → entity-start, breaks rendering).
    """
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Verification source → label + colour. Mirrors SOURCE_BADGE on the web result
# page so the buyer + procurement evaluator see the same attribution wherever
# the kit is viewed. Procurement officers reading the Cover Sheet (the
# audit-grade, blockchain-anchored doc) get the same per-answer evidence the
# buyer saw online — that's what makes the kit defensible end-to-end.
_BADGE_TEAL    = colors.HexColor("#0d9488")
_BADGE_BLUE    = colors.HexColor("#0284c7")
_BADGE_AMBER   = colors.HexColor("#d97706")
_VERIFICATION_BADGES: dict[str, tuple[str, "colors.Color"]] = {
    "intake":           ("FROM YOUR INTAKE",          EMERALD),
    "website":          ("VERIFIED ON YOUR WEBSITE",  _BADGE_TEAL),
    "intake+website":   ("INTAKE + WEBSITE",          EMERALD),
    "intake+external":  ("INTAKE + PUBLIC RECORDS",   EMERALD),
    "acra":             ("ACRA VERIFIED",             _BADGE_BLUE),
    "ssl":              ("SSL LABS VERIFIED",         _BADGE_BLUE),
    "gebiz":            ("GEBIZ SUPPLIER",            _BADGE_BLUE),
    "pdpc":             ("PDPC REGISTER CHECKED",     _BADGE_BLUE),
    "external":         ("EXTERNAL EVIDENCE",         _BADGE_BLUE),
    "ai_drafted":       ("AI DRAFT — REVIEW",         _BADGE_AMBER),
}


def _rfp_qa_block(idx: int, qa: dict):
    """Render a single RFP Q&A entry: question, answer, verification-source
    badge, and the evidence line that justifies the badge.
    """
    question = _xml_escape(qa.get("question") or "—")
    answer = _xml_escape(qa.get("answer") or "—")

    # Prefer the new structured verification field. Fall back to the legacy
    # `confidence` field for backward compatibility with kits generated
    # before v4 (existing Cover Sheets that get re-rendered).
    verification = qa.get("verification") if isinstance(qa.get("verification"), dict) else {}
    source = verification.get("source") or (
        "intake" if (qa.get("confidence") or "").lower() == "fact" else "ai_drafted"
    )
    evidence_list = verification.get("evidence") or []
    badge_text, badge_color = _VERIFICATION_BADGES.get(source, _VERIFICATION_BADGES["ai_drafted"])

    q_style = ParagraphStyle(
        "qa_q", fontSize=8.5, leading=11, textColor=NAVY, fontName="Helvetica-Bold"
    )
    a_style = ParagraphStyle(
        "qa_a", fontSize=8, leading=11, textColor=colors.HexColor("#334155")
    )
    evidence_style = ParagraphStyle(
        "qa_ev", fontSize=7, leading=9, textColor=SLATE,
        fontName="Helvetica-Oblique", leftIndent=4,
    )
    badge_style = ParagraphStyle(
        "qa_badge", fontSize=6, leading=8, textColor=WHITE,
        fontName="Helvetica-Bold", alignment=1,
    )

    # Badge wider than before because "INTAKE + PUBLIC RECORDS" / similar
    # combined labels don't fit in 0.9 inch.
    header = Table(
        [[
            Paragraph(f"Q{idx}. {question}", q_style),
            Paragraph(badge_text, badge_style),
        ]],
        colWidths=[4.7 * inch, 1.6 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (1, 0), (1, 0), badge_color),
        ("TEXTCOLOR", (1, 0), (1, 0), WHITE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    # Body holds the answer + (when present) the evidence line directly under it.
    body_rows = [[Paragraph(answer, a_style)]]
    if evidence_list:
        ev_text = "Evidence: " + " · ".join(_xml_escape(e) for e in evidence_list[:4])
        body_rows.append([Paragraph(ev_text, evidence_style)])
    body = Table(body_rows, colWidths=[6.3 * inch])
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("LINEBEFORE", (0, 0), (0, -1), 2, badge_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    return KeepTogether([header, body])


def generate_cover_sheet(data: Dict[str, Any]) -> bytes:
    """
    Build and return the cover sheet PDF bytes.

    Expected keys in `data`:
      company_name, customer_email, report_id,
      pdpa_status, pdpa_score, pdpa_details, pdpa_tx_hash,
      rfp_status, rfp_details (product_type, qa_count, generated_at, download_url), rfp_tx_hash,
      tx_hash, network,
      anchored_documents (signed cover sheet uploaded by user, post-issue),
      trm_domains (list of {domain, status, risk_rating}),
      recommendations (list of str),
      bundle_type,
    """
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_H + 0.3 * inch,
        bottomMargin=FOOTER_H + 0.3 * inch,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=_draw_page)])

    story = []
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    company = data.get("company_name", "Your Organisation")
    bundle_type = data.get("bundle_type", "compliance_evidence_pack")

    # ── Section 1: Cover ──────────────────────────────────────────────────────
    # The header bar logo is small and sits on navy — easy to miss. Drop a
    # prominent body logo on the cover page itself so the report is visibly
    # branded the moment the reader opens it.
    if _LOGO_PATH:
        try:
            story.append(Spacer(1, 0.05 * inch))
            story.append(Image(_LOGO_PATH, width=2.4 * inch, height=0.82 * inch, kind="proportional"))
            story.append(Spacer(1, 0.12 * inch))
        except Exception as e:
            logger.warning(f"[CoverSheet] Body logo render failed: {e}")
    story.append(Paragraph(f"Compliance Evidence Pack", _STYLES["h1"]))
    story.append(Paragraph(f"Summary Cover Sheet — {company}", _STYLES["caption"]))
    story.append(Spacer(1, 0.12 * inch))

    # Hero score badge — quick at-a-glance compliance posture
    pdpa_score_val = data.get("pdpa_score")
    score_display = f"{pdpa_score_val}/100" if isinstance(pdpa_score_val, int) else "Pending"
    score_color = (
        EMERALD if isinstance(pdpa_score_val, int) and pdpa_score_val >= 70
        else colors.HexColor("#f59e0b") if isinstance(pdpa_score_val, int) and pdpa_score_val >= 40
        else colors.HexColor("#ef4444") if isinstance(pdpa_score_val, int)
        else SLATE
    )
    anchored_count = len(data.get("anchored_documents") or [])
    hero_label = ParagraphStyle("hero_label", fontSize=7, leading=9, textColor=SLATE, alignment=1)
    hero_value = ParagraphStyle("hero_value", fontSize=18, leading=22, textColor=NAVY, fontName="Helvetica-Bold", alignment=1)
    hero_score = ParagraphStyle("hero_score", fontSize=18, leading=22, textColor=score_color, fontName="Helvetica-Bold", alignment=1)
    hero_data = [[
        Paragraph("PDPA COMPLIANCE", hero_label),
        Paragraph("DOCUMENTS ANCHORED", hero_label),
        Paragraph("BUNDLE STATUS", hero_label),
    ], [
        Paragraph(score_display, hero_score),
        Paragraph(str(anchored_count), hero_value),
        Paragraph("Active", hero_value),
    ]]
    hero = Table(hero_data, colWidths=[2.23 * inch, 2.23 * inch, 2.23 * inch])
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER", (0, 0), (1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
    ]))
    story.append(hero)
    story.append(Spacer(1, 0.12 * inch))

    story.append(_kv_table([
        ("Report ID", data.get("report_id", "—")),
        ("Generated", now),
        ("Bundle", bundle_type.replace("_", " ").title()),
        ("Prepared for", data.get("customer_email", "—")),
    ]))

    # ── Section 2: Components Delivered ───────────────────────────────────────
    story += _section("Bundle Components Delivered", _STYLES)
    signed_cs_tx = data.get("signed_cs_tx")
    signed_cs_hash = data.get("signed_cs_hash")
    signed_status = (
        "Anchored on-chain" if signed_cs_tx
        else "Awaiting your signature — sign this PDF and upload at booppa.io/compliance/cover-sheet"
    )
    components = [
        ("PDPA Quick Scan Report", data.get("pdpa_status", "Queued")),
        ("RFP Complete Kit", data.get("rfp_status", "Queued")),
        ("Compliance Summary Cover Sheet", "This document — anchored at issue"),
        ("Signed Cover Sheet (1× notarization)", signed_status),
    ]
    story.append(_kv_table(components))

    # ── Section 3: PDPA Status ─────────────────────────────────────────────────
    story += _section("PDPA Compliance Status", _STYLES, page_break=True)
    pdpa_score_v = data.get("pdpa_score")
    score_str = f"{pdpa_score_v} / 100" if isinstance(pdpa_score_v, int) else "Pending — scan still running"
    pdpa_d = data.get("pdpa_details") or {}
    sev = pdpa_d.get("severity_counts") or {}
    sev_summary = (
        f"{sev.get('High', 0)} High · {sev.get('Medium', 0)} Medium · {sev.get('Low', 0)} Low"
        if sev else "—"
    )
    laws = pdpa_d.get("detected_laws") or []
    laws_str = ", ".join(laws) if laws else "PDPA (Singapore) 2012"
    pdpa_rows = [
        ("Status", data.get("pdpa_status", "Pending")),
        ("Compliance Score", score_str),
        ("Scanned URL", pdpa_d.get("website_url") or "—"),
        ("Risk Level", str(pdpa_d.get("risk_level") or "—").title()),
        ("Findings", f"{pdpa_d.get('total_findings', 0)} total — {sev_summary}"),
        ("Frameworks Detected", laws_str),
    ]
    story.append(_kv_table(pdpa_rows))

    exec_sum = pdpa_d.get("executive_summary")
    if exec_sum:
        story.append(Spacer(1, 0.05 * inch))
        story.append(Paragraph("<b>Executive Summary</b>", _STYLES["caption"]))
        story.append(Paragraph(exec_sum, _STYLES["caption"]))

    # Full findings list — one card per finding with severity, description,
    # legislation and remediation. No truncation: this PDF is the customer's
    # complete evidence record.
    findings_full = pdpa_d.get("findings") or []
    if findings_full:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            f"<b>Detailed Findings ({len(findings_full)})</b>", _STYLES["caption"]
        ))
        story.append(Spacer(1, 0.04 * inch))
        for idx, f in enumerate(findings_full, 1):
            story.append(_pdpa_finding_block(idx, f))
            story.append(Spacer(1, 0.06 * inch))

    # ── Section 4: RFP Complete Summary ───────────────────────────────────────
    story += _section("RFP Complete Summary", _STYLES, page_break=True)
    rfp_d = data.get("rfp_details") or {}
    generated_at = rfp_d.get("generated_at") or "—"
    if isinstance(generated_at, str) and "T" in generated_at:
        generated_at = generated_at.split("T")[0]
    qa_count = rfp_d.get("qa_count")
    qa_str = f"{qa_count} questions answered" if isinstance(qa_count, int) and qa_count > 0 else "—"
    answer_source = str(rfp_d.get("answer_source") or "ai_grounded").replace("_", " ").title()
    download_url = rfp_d.get("download_url") or "—"
    download_str = "Available — see email" if download_url and download_url != "—" else "Pending generation"
    rfp_rows = [
        ("Status", data.get("rfp_status", "Pending")),
        ("Product", str(rfp_d.get("product_type") or "rfp_complete").replace("_", " ").title()),
        # `&` must be the XML entity in Paragraph text or ReportLab mangles it.
        ("Q&amp;A Coverage", qa_str),
        ("Answer Source", answer_source),
        ("Generated", generated_at),
        ("Bid Kit Download", download_str),
    ]
    story.append(_kv_table(rfp_rows))

    discrepancies = rfp_d.get("discrepancies") or []
    if discrepancies:
        story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph("<b>Discrepancies flagged for review</b>", _STYLES["caption"]))
        for d in discrepancies[:5]:
            label = d if isinstance(d, str) else d.get("description") or d.get("title") or str(d)
            story.append(Paragraph(f"• {label}", _STYLES["caption"]))
            story.append(Spacer(1, 2))

    rfp_exec = rfp_d.get("executive_summary")
    if rfp_exec:
        story.append(Spacer(1, 0.05 * inch))
        story.append(Paragraph("<b>Executive Summary</b>", _STYLES["caption"]))
        story.append(Paragraph(rfp_exec, _STYLES["caption"]))

    # Full Q&A — every answer the kit generated, with confidence labels.
    # Procurement officers can verify the buyer's bid claims against this list
    # without leaving the cover sheet.
    qa_answers = rfp_d.get("qa_answers") or []
    if qa_answers:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            f"<b>RFP Q&amp;A ({len(qa_answers)})</b>", _STYLES["caption"]
        ))
        story.append(Spacer(1, 0.04 * inch))
        for idx, qa in enumerate(qa_answers, 1):
            story.append(_rfp_qa_block(idx, qa))
            story.append(Spacer(1, 0.05 * inch))

    # ── Section 5: Blockchain Evidence Trail ──────────────────────────────────
    story += _section("Blockchain Evidence Trail", _STYLES, page_break=True)
    from app.core.config import settings
    network = data.get("network", settings.POLYGON_NETWORK_NAME)
    explorer = settings.POLYGON_EXPLORER_URL.rstrip("/")
    mono = ParagraphStyle("mono", fontSize=7.5, leading=10, textColor=colors.HexColor("#334155"), fontName="Courier")

    def _anchor_row(label: str, tx: str | None) -> tuple[str, Any]:
        if not tx or tx == "—":
            return (label, Paragraph("Pending anchor", _STYLES["caption"]))
        short = (tx[:10] + "…" + tx[-8:]) if len(tx) > 24 else tx
        url = f"{explorer}/tx/{tx}"
        return (label, Paragraph(f"{short}<br/><font size='6'>{url}</font>", mono))

    pdpa_tx = data.get("pdpa_tx_hash")
    rfp_tx = data.get("rfp_tx_hash")
    cs_tx = data.get("tx_hash")

    rows = [
        ("Network", network),
        ("Anchoring Standard", "SHA-256 → EvidenceAnchorV3 smart contract"),
        _anchor_row("PDPA Snapshot", pdpa_tx),
        _anchor_row("RFP Complete Kit", rfp_tx),
        _anchor_row("Cover Sheet (this PDF)", cs_tx),
    ]
    if signed_cs_tx:
        rows.append(_anchor_row("Signed Cover Sheet", signed_cs_tx))
        if signed_cs_hash:
            short_h = (signed_cs_hash[:18] + "…" + signed_cs_hash[-6:]) if len(signed_cs_hash) > 28 else signed_cs_hash
            rows.append(("Signed Cover Sheet SHA-256", Paragraph(short_h, mono)))
    else:
        rows.append((
            "Signed Cover Sheet",
            Paragraph(
                "<b>Pending manual upload of signed Cover Sheet.</b><br/>"
                "Sign this PDF, then upload the signed copy at booppa.io/compliance/cover-sheet "
                "using your 1 included credit. Once anchored, this row updates with the tx.",
                _STYLES["caption"],
            ),
        ))
    story.append(_kv_table(rows))

    # ── Section 5b: Anchored Compliance Documents ────────────────────────────
    anchored_docs = data.get("anchored_documents") or []
    if anchored_docs:
        story += _section("Anchored Compliance Documents", _STYLES)
        doc_rows = [["#", "Document", "SHA-256 Hash", "Tx"]]
        for i, d in enumerate(anchored_docs, 1):
            descriptor = d.get("descriptor") or d.get("filename") or "—"
            file_hash = d.get("file_hash") or "—"
            short_hash = (file_hash[:18] + "…" + file_hash[-6:]) if len(file_hash) > 28 else file_hash
            tx = d.get("tx_hash") or ""
            short_tx = (tx[:12] + "…" + tx[-6:]) if tx else "Pending"
            doc_rows.append([str(i), descriptor[:48], short_hash, short_tx])
        doc_table = Table(doc_rows, colWidths=[0.3 * inch, 2.6 * inch, 2.5 * inch, 1.3 * inch])
        doc_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (2, 1), (3, -1), "Courier"),
            ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [LIGHT, WHITE]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(doc_table)
        story.append(Spacer(1, 0.05 * inch))
        story.append(Paragraph(
            "Each document above has been hashed with SHA-256 and anchored to the "
            f"{data.get('network', 'Polygon')} network. Verify at "
            "booppa.io/verify/&lt;hash&gt; or via the linked Polygonscan transaction.",
            _STYLES["small"],
        ))

    # ── Section 6: MAS TRM Assessment ─────────────────────────────────────────
    trm_domains = data.get("trm_domains", [])
    if trm_domains:
        story += _section("MAS TRM Assessment Overview", _STYLES)
        trm_rows = [["Domain", "Status", "Risk"]]
        for d in trm_domains:
            trm_rows.append([d.get("domain", ""), d.get("status", "not_started"), d.get("risk_rating") or "—"])
        trm_table = Table(trm_rows, colWidths=[3.5 * inch, 1.8 * inch, 1.4 * inch])
        trm_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [LIGHT, WHITE]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(trm_table)

    # ── Section 7: Risk Overview ───────────────────────────────────────────────
    story += _section("Risk Overview", _STYLES, page_break=True)
    story.append(Paragraph(
        "This pack represents a snapshot assessment. Booppa's automated scans "
        "cover PDPA obligations and vendor credibility signals. "
        "A qualified DPO should review findings before submission to regulators.",
        _STYLES["caption"],
    ))

    # ── Section 8: Recommendations ────────────────────────────────────────────
    recommendations = data.get("recommendations") or [
        "Address any PDPA gaps identified in your scan report within 30 days.",
        "Upload updated policy documents and re-anchor via Booppa Notarization.",
        "Schedule a full MAS TRM gap analysis for all 13 domains.",
        "Enable SSO and set data retention policies in your Enterprise dashboard.",
    ]
    story += _section("Recommendations", _STYLES)
    for i, rec in enumerate(recommendations, 1):
        story.append(Paragraph(f"{i}. {rec}", _STYLES["caption"]))
        story.append(Spacer(1, 3))

    # ── Section 9: Legal Disclaimer ────────────────────────────────────────────
    story += _section("Legal Disclaimer", _STYLES)
    story.append(Paragraph(
        "This document is generated by Booppa Smart Care LLC (UEN: 202506025W) for informational "
        "purposes only. It does not constitute legal advice. The blockchain anchors provide "
        "evidence of document existence at a point in time but do not guarantee regulatory "
        "compliance. © Booppa Smart Care LLC. All rights reserved.",
        _STYLES["small"],
    ))

    doc.build(story)
    return buf.getvalue()
