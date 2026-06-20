"""
PDPA Evidence Pack — PDF Builder

Generates branded DRAFT PDFs for all 7 compliance documents. Uses ReportLab.
Anchoring disclosure is honest about the network (Polygon Amoy testnet); the
fabricated Booppa UEN is not printed (Booppa has no SG UEN).
"""

import io
import json
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

from app.core.config import settings
from app.core.company import COMPANY_NAME


try:
    import qrcode  # optional — QR is a nice-to-have, not required
    _HAS_QR = True
except Exception:  # pragma: no cover
    _HAS_QR = False

# ── BRAND COLOURS ────────────────────────────────────────────────────────
TEAL        = colors.HexColor("#00C9A7")
INK         = colors.HexColor("#0A0F1E")
MUTED       = colors.HexColor("#6B7280")
RULE        = colors.HexColor("#E2E0DB")
PAPER       = colors.HexColor("#F5F4F0")
RED         = colors.HexColor("#FF4444")
AMBER       = colors.HexColor("#F59E0B")

W, H        = A4
MARGIN      = 18 * mm
INNER_W     = W - 2 * MARGIN

# ── STYLES ───────────────────────────────────────────────────────────────

def _make_styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1",
            fontName="Helvetica-Bold", fontSize=22, leading=28,
            textColor=INK, spaceAfter=6),
        "h2": ParagraphStyle("h2",
            fontName="Helvetica-Bold", fontSize=14, leading=18,
            textColor=INK, spaceAfter=4, spaceBefore=12),
        "h3": ParagraphStyle("h3",
            fontName="Helvetica-Bold", fontSize=11, leading=14,
            textColor=INK, spaceAfter=3, spaceBefore=8),
        "body": ParagraphStyle("body",
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=INK, spaceAfter=4),
        "small": ParagraphStyle("small",
            fontName="Helvetica", fontSize=8, leading=11,
            textColor=MUTED, spaceAfter=3),
        "mono": ParagraphStyle("mono",
            fontName="Courier", fontSize=7, leading=10,
            textColor=MUTED, spaceAfter=2),
        "label": ParagraphStyle("label",
            fontName="Helvetica-Bold", fontSize=7, leading=9,
            textColor=TEAL, spaceAfter=2, spaceBefore=6),
        "teal_bold": ParagraphStyle("teal_bold",
            fontName="Helvetica-Bold", fontSize=9, leading=12,
            textColor=TEAL),
        "center": ParagraphStyle("center",
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=MUTED, alignment=TA_CENTER),
    }

S = _make_styles()


# ── QR CODE ──────────────────────────────────────────────────────────────

def _make_qr(data: str, size_mm: int = 30) -> Image:
    qr = qrcode.QRCode(version=1, box_size=4, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0A0F1E", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    size = size_mm * mm
    return Image(buf, width=size, height=size)


# ── HEADER / FOOTER ──────────────────────────────────────────────────────

def _header_footer(canvas, doc):
    canvas.saveState()
    # Header bar
    canvas.setFillColor(INK)
    canvas.rect(0, H - 14*mm, W, 14*mm, fill=1, stroke=0)
    canvas.setFillColor(TEAL)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(MARGIN, H - 8*mm, "BOOPPA INTELLIGENCE")
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(W - MARGIN, H - 8*mm, "PDPA Compliance Evidence Pack · BCEP-v1.1")
    # Footer
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 6.5)
    footer_text = (f"Automated compliance documentation by {COMPANY_NAME} · "
                   "Results based on information provided at assessment date. "
                   "Does not substitute for legal counsel.")
    canvas.drawString(MARGIN, 8*mm, footer_text)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(W - MARGIN, 8*mm, f"Page {doc.page}")
    canvas.restoreState()


# ── COVER PAGE ───────────────────────────────────────────────────────────

def _cover_page(pack: dict, doc_meta: dict) -> list:
    elements = []
    elements.append(Spacer(1, 30*mm))

    # Document badge
    elements.append(Paragraph(
        f"DOCUMENT {doc_meta['number']} OF 7 &nbsp;·&nbsp; {doc_meta['ref']}",
        S["label"]
    ))
    elements.append(Spacer(1, 4*mm))

    elements.append(Paragraph(doc_meta["title"], S["h1"]))
    elements.append(HRFlowable(width=INNER_W, color=TEAL, thickness=2, spaceAfter=6*mm))

    # Org block — assessed entity is the CUSTOMER (incl. their UEN).
    info_data = [
        ["Organisation",   pack["organisation"]],
        ["UEN",            pack.get("uen", "Not provided")],
        ["Pack ID",        pack["pack_id"]],
        ["Framework",      pack["framework"]],
        ["Generated",      pack["generated_at"][:10]],
        ["Next Review",    doc_meta.get("next_review", "12 months from effective date")],
        ["Status",         "ANCHORED · Polygon Amoy testnet"],
    ]
    info_table = Table(info_data, colWidths=[45*mm, INNER_W - 45*mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME",    (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME",    (1,0), (1,-1), "Helvetica"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("TEXTCOLOR",   (0,0), (0,-1), MUTED),
        ("TEXTCOLOR",   (1,0), (1,-1), INK),
        ("LINEBELOW",   (0,0), (-1,-2), 0.3, RULE),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 10*mm))

    # Blockchain anchoring box
    anchor = pack.get("anchoring", {}).get(doc_meta["doc_type"], {})
    tx_hash   = anchor.get("tx_hash", "PENDING")
    doc_hash  = pack.get("hashes", {}).get(doc_meta["doc_type"], "")
    explorer_base = settings.active_polygon_explorer_url.rstrip("/")
    verify_url = anchor.get("verification_url") or (
        f"{explorer_base}/tx/{tx_hash}" if tx_hash and tx_hash != "PENDING" else "Pending"
    )

    chain_data = [
        ["SHA-256 Hash",    doc_hash or "Computing..."],
        ["Transaction",     tx_hash],
        ["Anchored On",     "Polygon Amoy testnet"],
        ["Anchor Time",     anchor.get("anchor_time_utc", "Pending")[:19].replace("T", " ") + " UTC"],
        ["Verify At",       verify_url],
    ]
    chain_table = Table(chain_data, colWidths=[35*mm, INNER_W - 35*mm])
    chain_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), INK),
        ("TEXTCOLOR",   (0,0), (0,-1), TEAL),
        ("TEXTCOLOR",   (1,0), (1,-1), colors.white),
        ("FONTNAME",    (0,0), (-1,-1), "Courier"),
        ("FONTSIZE",    (0,0), (-1,-1), 7),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [INK, colors.HexColor("#111827")]),
    ]))
    elements.append(chain_table)
    elements.append(Spacer(1, 6*mm))

    # QR code
    if _HAS_QR and tx_hash and tx_hash != "PENDING":
        qr_img = _make_qr(verify_url, size_mm=28)
        qr_label = Paragraph("Scan to verify on the Polygon Amoy testnet explorer · No login required", S["center"])
        qr_table = Table([[qr_img, qr_label]], colWidths=[32*mm, INNER_W - 32*mm])
        qr_table.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
        ]))
        elements.append(qr_table)

    elements.append(Spacer(1, 6*mm))

    # Draft status warning — always shown until client signs
    is_critical = doc_meta.get("verification_critical", False)
    warning_color = colors.HexColor("#FF4444") if is_critical else colors.HexColor("#F59E0B")
    warning_label = "CRITICAL VERIFICATION REQUIRED" if is_critical else "VERIFICATION REQUIRED BEFORE EVIDENTIARY USE"
    warning_text = (
        "This document is an AI-generated DRAFT. It has NO evidentiary value until the "
        "authorised representative of the organisation reviews, corrects, and signs it. "
        "The signed PDF is then SHA-256 hashed and anchored on the Booppa evidence chain "
        "(Polygon Amoy testnet). Do not present this document to PDPC or any regulator in "
        "unsigned form."
    )
    warning_data = [[
        Paragraph(warning_label, ParagraphStyle("warn_label",
            fontName="Helvetica-Bold", fontSize=8, textColor=colors.white)),
        Paragraph(warning_text, ParagraphStyle("warn_body",
            fontName="Helvetica", fontSize=7.5, textColor=colors.white, leading=11)),
    ]]
    warning_table = Table(warning_data, colWidths=[50*mm, INNER_W - 50*mm])
    warning_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), warning_color),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("RIGHTPADDING",  (0,0), (-1,-1), 10),
        ("ROUNDEDCORNERS", [4]),
    ]))
    elements.append(warning_table)

    elements.append(PageBreak())
    return elements


# ── DOCUMENT RENDERERS ───────────────────────────────────────────────────

def _render_generic_section(title: str, data: dict, depth: int = 0) -> list:
    """Recursively render JSON sections as formatted paragraphs."""
    elements = []
    indent = depth * 4 * mm

    if isinstance(data, dict):
        for key, value in data.items():
            label = key.replace("_", " ").title()
            if isinstance(value, (dict, list)):
                elements.append(Paragraph(label, S["h3"] if depth == 0 else S["label"]))
                elements.extend(_render_generic_section(label, value, depth + 1))
            else:
                row = Table(
                    [[Paragraph(label, S["label"]),
                      Paragraph(str(value), S["body"])]],
                    colWidths=[50*mm, INNER_W - 50*mm - indent]
                )
                row.setStyle(TableStyle([
                    ("VALIGN",        (0,0), (-1,-1), "TOP"),
                    ("LEFTPADDING",   (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                    ("TOPPADDING",    (0,0), (-1,-1), 2),
                    ("LINEBELOW",     (0,0), (-1,-1), 0.2, RULE),
                ]))
                elements.append(row)

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                elements.append(HRFlowable(width=INNER_W - indent, color=RULE,
                                           thickness=0.5, spaceAfter=2))
                elements.extend(_render_generic_section(f"Item {i+1}", item, depth))
            else:
                elements.append(Paragraph(f"• {item}", S["body"]))
    else:
        elements.append(Paragraph(str(data), S["body"]))

    return elements


def _render_inventory_table(inventory: list) -> list:
    """Special renderer for data inventory — shows as compact table."""
    elements = []
    headers = ["Category", "Purpose", "Legal Basis", "Retention", "Location"]
    rows = [headers]
    for item in inventory:
        rows.append([
            item.get("category", ""),
            item.get("purpose", ""),
            item.get("legal_basis", ""),
            item.get("retention_period", ""),
            item.get("storage_location", ""),
        ])

    col_w = [35*mm, 40*mm, 35*mm, 30*mm, INNER_W - 145*mm]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), INK),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME",      (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE",      (0,0), (-1,-1), 7.5),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, PAPER]),
        ("GRID",          (0,0), (-1,-1), 0.3, RULE),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))
    elements.append(t)
    return elements


# ── MAIN PDF BUILDER ─────────────────────────────────────────────────────

DOC_META = {
    "dpmp": {
        "number": "01", "doc_type": "dpmp",
        "title": "Data Protection Management Programme",
        "ref": "PDPA §11 · Openness Obligation",
        "next_review": "12 months from effective date",
        "verification_critical": False,
    },
    "ropa": {
        "number": "02", "doc_type": "ropa",
        "title": "Record of Processing Activities (ROPA)",
        "ref": "PDPA §11 · §18 · §25 · §26",
        "next_review": "Every 6 months or upon new processing activity",
        "verification_critical": True,  # Most inspection-sensitive document
    },
    "data_inventory": {
        "number": "03", "doc_type": "data_inventory",
        "title": "Data Inventory & Retention Schedule",
        "ref": "PDPA §25 · Retention Limitation",
        "next_review": "Annual",
        "verification_critical": False,
    },
    "vendor_register": {
        "number": "04", "doc_type": "vendor_register",
        "title": "Third-Party Processor Register & DPA Checklist",
        "ref": "PDPA §26 · Cross-Border Transfer",
        "next_review": "Annual or upon new vendor",
        "verification_critical": False,
    },
    "breach_runbook": {
        "number": "05", "doc_type": "breach_runbook",
        "title": "Data Breach Response Runbook",
        "ref": "PDPA §26B-D · Mandatory Breach Notification",
        "next_review": "Annual + post-incident",
        "verification_critical": False,
    },
    "training": {
        "number": "06", "doc_type": "training",
        "title": "Staff Training Register & Completion Evidence",
        "ref": "PDPA Accountability Obligation",
        "next_review": "Annual",
        "verification_critical": True,  # Must reflect actual training records
    },
    "review_log": {
        "number": "07", "doc_type": "review_log",
        "title": "Periodic Security Review Log",
        "ref": "PDPA §24 · Protection Obligation",
        "next_review": "Quarterly / Semi-annual / Annual by review type",
        "verification_critical": False,
    },
}


def build_single_pdf(pack: dict, doc_type: str, output_path: str) -> str:
    """Build a single document PDF."""
    meta    = DOC_META[doc_type]
    doc_data = pack["documents"].get(doc_type, {})

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf if not output_path else output_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=18*mm, bottomMargin=16*mm,
        title=f"{meta['title']} — {pack['organisation']}",
        author=COMPANY_NAME,
    )

    elements = []

    # Cover page
    elements.extend(_cover_page(pack, meta))

    # Document title
    elements.append(Paragraph(meta["title"], S["h1"]))
    elements.append(Paragraph(meta["ref"], S["label"]))
    elements.append(HRFlowable(width=INNER_W, color=TEAL, thickness=1.5, spaceAfter=6*mm))

    # Render content
    if doc_type == "data_inventory" and "inventory" in doc_data:
        # Special table renderer for inventory
        elements.append(Paragraph("Data Inventory", S["h2"]))
        elements.extend(_render_inventory_table(doc_data["inventory"]))
        elements.append(Spacer(1, 6*mm))
        # Render remaining fields normally
        remaining = {k: v for k, v in doc_data.items() if k != "inventory"}
        elements.extend(_render_generic_section("", remaining))
    else:
        elements.extend(_render_generic_section("", doc_data))

    # Sign-off block
    elements.append(Spacer(1, 10*mm))
    elements.append(HRFlowable(width=INNER_W, color=RULE, thickness=0.5))
    elements.append(Spacer(1, 4*mm))

    approval = pack.get("approvals", {}).get(doc_type, {})
    if approval:
        elements.append(Paragraph("MANAGEMENT APPROVAL", S["label"]))
        sign_data = [
            ["Approved by",  approval.get("approver_email", "")],
            ["Approved at",  approval.get("approved_at", "")[:19].replace("T"," ") + " UTC"],
            ["Approval hash", approval.get("approval_hash", "")],
            ["Anchor TX",    approval.get("anchor", {}).get("tx_hash", "pending")],
        ]
        sign_table = Table(sign_data, colWidths=[35*mm, INNER_W - 35*mm])
        sign_table.setStyle(TableStyle([
            ("FONTNAME",    (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("TEXTCOLOR",   (0,0), (0,-1), MUTED),
            ("FONTNAME",    (1,0), (1,0), "Helvetica"),
            ("FONTNAME",    (1,2), (1,-1), "Courier"),
            ("FONTSIZE",    (1,2), (1,-1), 7),
            ("TOPPADDING",  (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        elements.append(sign_table)
    else:
        elements.append(Paragraph(
            "Awaiting management approval — approval will be anchored separately on the Booppa evidence chain.",
            S["small"]
        ))

    doc.build(elements, onFirstPage=_header_footer, onLaterPages=_header_footer)

    if output_path:
        return output_path
    return buf.getvalue()


def build_evidence_pack_pdfs(pack: dict, output_dir: str = "/tmp") -> dict:
    """
    Build all 6 PDFs for an Evidence Pack.
    Returns dict of {doc_type: file_path}.
    """
    import os
    os.makedirs(output_dir, exist_ok=True)

    paths = {}
    for doc_type in DOC_META:
        if doc_type not in pack.get("documents", {}):
            print(f"  Skipping PDF for {doc_type} — not generated")
            continue

        out_path = os.path.join(output_dir, f"{pack['pack_id']}_{doc_type}.pdf")
        print(f"  Building PDF: {doc_type}...")
        build_single_pdf(pack, doc_type, out_path)
        paths[doc_type] = out_path
        print(f"  Done: {out_path}")

    return paths
