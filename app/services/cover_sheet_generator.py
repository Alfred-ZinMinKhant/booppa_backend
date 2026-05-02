"""
Compliance Evidence Pack — Summary Cover Sheet Generator
=========================================================
9-section ReportLab PDF delivered after all bundle components are ready.

Sections:
  1. Cover / Executive Summary
  2. Bundle Components Delivered
  3. PDPA Compliance Status
  4. Vendor Proof Summary
  5. Blockchain Evidence Trail
  6. MAS TRM Assessment (if available)
  7. Risk Overview
  8. Recommendations
  9. Legal Disclaimer
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional, Dict, Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
HEADER_H = 0.55 * inch
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

    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(MARGIN, PAGE_H - HEADER_H + 0.18 * inch, "BOOPPA")
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(MARGIN + 0.6 * inch, PAGE_H - HEADER_H + 0.18 * inch, "Compliance Evidence Pack")

    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.18 * inch, "COVER SHEET")

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, "Booppa Smart Care LLC · booppa.io · Confidential")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


def _section(title: str, styles) -> list:
    return [
        Spacer(1, 0.15 * inch),
        Paragraph(f'<font color="#10b981">■</font>  <b>{title}</b>', styles["h2"]),
        HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6),
    ]


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", _STYLES["Normal"]), Paragraph(str(v), _STYLES["Normal"])] for k, v in rows]
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
    "h2": ParagraphStyle("cs_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4),
    "small": ParagraphStyle("cs_small", fontSize=7, leading=10, textColor=SLATE),
    "caption": ParagraphStyle("cs_caption", fontSize=9, leading=13, textColor=colors.HexColor("#334155")),
}


def generate_cover_sheet(data: Dict[str, Any]) -> bytes:
    """
    Build and return the cover sheet PDF bytes.

    Expected keys in `data`:
      company_name, customer_email, report_id,
      pdpa_status, pdpa_score,
      vendor_proof_status,
      tx_hash, network,
      notarization_count,
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
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(f"Compliance Evidence Pack", _STYLES["h1"]))
    story.append(Paragraph(f"Summary Cover Sheet — {company}", _STYLES["caption"]))
    story.append(Spacer(1, 0.05 * inch))
    story.append(_kv_table([
        ("Report ID", data.get("report_id", "—")),
        ("Generated", now),
        ("Bundle", bundle_type.replace("_", " ").title()),
        ("Prepared for", data.get("customer_email", "—")),
    ]))

    # ── Section 2: Components Delivered ───────────────────────────────────────
    story += _section("Bundle Components Delivered", _STYLES)
    anchored_docs = data.get("anchored_documents") or []
    notarization_count = len(anchored_docs) if anchored_docs else data.get("notarization_count", 0)
    notarization_status = (
        f"{notarization_count} document(s) anchored on-chain"
        if anchored_docs
        else "Available — upload at booppa.io/compliance-evidence-pack/upload"
    )
    components = [
        ("Vendor Proof Certificate", data.get("vendor_proof_status", "Queued")),
        ("PDPA Quick Scan Report", data.get("pdpa_status", "Queued")),
        (f"Notarized Compliance Documents ({notarization_count}×)", notarization_status),
        ("Compliance Summary Cover Sheet", "This document"),
    ]
    story.append(_kv_table(components))

    # ── Section 3: PDPA Status ─────────────────────────────────────────────────
    story += _section("PDPA Compliance Status", _STYLES)
    story.append(_kv_table([
        ("Status", data.get("pdpa_status", "Pending")),
        ("Compliance Score", f"{data.get('pdpa_score', '—')}%"),
        ("Framework", "PDPA (Singapore) 2012"),
        ("Assessment Type", "Automated + AI-assisted review"),
    ]))

    # ── Section 4: Vendor Proof ────────────────────────────────────────────────
    story += _section("Vendor Proof Summary", _STYLES)
    story.append(_kv_table([
        ("Status", data.get("vendor_proof_status", "Pending")),
        ("Company", company),
        ("Verification Level", "Basic (automated)"),
        ("Registry", "ACRA / GeBIZ"),
    ]))

    # ── Section 5: Blockchain Evidence Trail ──────────────────────────────────
    story += _section("Blockchain Evidence Trail", _STYLES)
    from app.core.config import settings
    tx = data.get("tx_hash", "—")
    network = data.get("network", settings.POLYGON_NETWORK_NAME)
    explorer = settings.POLYGON_EXPLORER_URL.rstrip("/")
    story.append(_kv_table([
        ("Network", network),
        ("Transaction Hash", tx),
        ("Verify URL", f"{explorer}/tx/{tx}" if tx != "—" else "Pending anchor"),
        ("Anchoring Standard", "SHA-256 → EvidenceAnchorV3 smart contract"),
    ]))

    # ── Section 5b: Anchored Compliance Documents ────────────────────────────
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
    story += _section("Risk Overview", _STYLES)
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
