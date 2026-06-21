"""
PDPA Evidence Pack — PDF Builder

Generates branded DRAFT PDFs for all 7 compliance documents. Uses ReportLab.
Anchoring disclosure is honest about the network (Polygon Amoy testnet); the
fabricated Booppa UEN is not printed (Booppa has no SG UEN).

Layout system (BCEP-v1.1 redesign):
  * Every cell rendered through ``_cell`` → a ``Paragraph`` (XML-escaped) so text
    always wraps instead of overflowing/overlapping its column.
  * ``_kv_table`` — flat label/value blocks. ``_records_table`` — list-of-dicts
    tables with proportional, INNER_W-normalised columns. ``_section_card`` —
    titled section block with a teal rule.
  * The Booppa logo appears both in the page header bar and centered on each
    document's cover page. All logo drawing is wrapped in try/except so a missing
    asset can never break a paid fulfillment artifact.
"""

import io
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

from app.core.config import settings
from app.core.company import COMPANY_NAME
from app.services.pdf_logo import LOGO_PATH


try:
    import qrcode  # optional — QR is a nice-to-have, not required
    _HAS_QR = True
except Exception:  # pragma: no cover
    _HAS_QR = False

# ── BRAND COLOURS ────────────────────────────────────────────────────────
TEAL        = colors.HexColor("#00C9A7")
TEAL_DARK   = colors.HexColor("#00A88B")
INK         = colors.HexColor("#0A0F1E")
INK_SOFT    = colors.HexColor("#1F2937")
MUTED       = colors.HexColor("#6B7280")
RULE        = colors.HexColor("#E2E0DB")
PAPER       = colors.HexColor("#F5F4F0")
CARD_BG     = colors.HexColor("#FAFAF8")
RED         = colors.HexColor("#FF4444")
AMBER       = colors.HexColor("#F59E0B")

W, H        = A4
MARGIN      = 18 * mm
INNER_W     = W - 2 * MARGIN

# ── STYLES ───────────────────────────────────────────────────────────────

def _make_styles():
    getSampleStyleSheet()  # ensure default stylesheet is initialised
    return {
        "h1": ParagraphStyle("h1",
            fontName="Helvetica-Bold", fontSize=22, leading=27,
            textColor=INK, spaceAfter=6),
        "h2": ParagraphStyle("h2",
            fontName="Helvetica-Bold", fontSize=13, leading=17,
            textColor=INK, spaceAfter=4, spaceBefore=14),
        "h3": ParagraphStyle("h3",
            fontName="Helvetica-Bold", fontSize=10.5, leading=14,
            textColor=INK_SOFT, spaceAfter=3, spaceBefore=8),
        "body": ParagraphStyle("body",
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=INK, spaceAfter=4),
        "bullet": ParagraphStyle("bullet",
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=INK, spaceAfter=2, leftIndent=8, bulletIndent=0),
        "caption": ParagraphStyle("caption",
            fontName="Helvetica", fontSize=8, leading=11,
            textColor=MUTED, spaceAfter=3),
        "th": ParagraphStyle("th",
            fontName="Helvetica-Bold", fontSize=7.5, leading=9.5,
            textColor=colors.white),
        "td": ParagraphStyle("td",
            fontName="Helvetica", fontSize=7.5, leading=10,
            textColor=INK),
        "kv_label": ParagraphStyle("kv_label",
            fontName="Helvetica-Bold", fontSize=8.5, leading=11,
            textColor=MUTED),
        "kv_value": ParagraphStyle("kv_value",
            fontName="Helvetica", fontSize=8.5, leading=12,
            textColor=INK),
        "mono": ParagraphStyle("mono",
            fontName="Courier", fontSize=7, leading=10,
            textColor=MUTED, spaceAfter=2),
        "label": ParagraphStyle("label",
            fontName="Helvetica-Bold", fontSize=7.5, leading=10,
            textColor=TEAL, spaceAfter=2, spaceBefore=4),
        "label_keep": ParagraphStyle("label_keep",
            fontName="Helvetica-Bold", fontSize=7.5, leading=10,
            textColor=TEAL, spaceAfter=2, spaceBefore=4, keepWithNext=1),
        "center": ParagraphStyle("center",
            fontName="Helvetica", fontSize=8.5, leading=12,
            textColor=MUTED, alignment=TA_CENTER),
    }

S = _make_styles()


# ── TEXT HELPERS ───────────────────────────────────────────────────────────

def _xml_escape(text: str) -> str:
    """Escape ``&``/``<``/``>`` so AI-generated strings are safe inside a
    ReportLab Paragraph (its mini-XML treats ``&`` and ``<`` as markup)."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _stringify(value) -> str:
    """Flatten a scalar / list / dict value into displayable text."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        parts = [_stringify(v) for v in value if v not in (None, "")]
        return " · ".join(parts) if parts else "—"
    if isinstance(value, dict):
        return " · ".join(
            f"{k.replace('_', ' ').title()}: {_stringify(v)}"
            for k, v in value.items()
        )
    return str(value)


def _cell(value, style=None) -> Paragraph:
    """Wrap any value in an XML-escaped Paragraph so it wraps within its column.

    Lists are rendered as line-broken bullets for readability inside a cell.
    """
    style = style or S["td"]
    if isinstance(value, list):
        items = [v for v in value if v not in (None, "")]
        if items:
            html = "<br/>".join("• " + _xml_escape(_stringify(v)) for v in items)
            return Paragraph(html, style)
        return Paragraph("—", style)
    return Paragraph(_xml_escape(_stringify(value)), style)


def _pretty(key: str) -> str:
    return key.replace("_", " ").title()


# ── LAYOUT PRIMITIVES ────────────────────────────────────────────────────

def _kv_table(rows: list, label_w: float = 45 * mm) -> Table:
    """2-column label/value table; every cell wrapped so long values flow."""
    data = [[_cell(k, S["kv_label"]), _cell(v, S["kv_value"])] for k, v in rows]
    t = Table(data, colWidths=[label_w, INNER_W - label_w])
    t.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.25, RULE),
    ]))
    return t


def _records_table(items: list, columns: list, weights: list = None) -> Table:
    """List-of-dicts → table with proportional columns normalised to INNER_W.

    ``columns`` is a list of dict keys; headers are the prettified keys. Every
    cell is wrapped via ``_cell`` (no overflow). Zebra striping + repeat header.
    """
    if not weights:
        weights = [1] * len(columns)
    total = float(sum(weights))
    col_w = [INNER_W * (w / total) for w in weights]

    header = [_cell(_pretty(c), S["th"]) for c in columns]
    rows = [header]
    for item in items:
        rows.append([_cell(item.get(c, ""), S["td"]) for c in columns])

    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), INK),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
        ("GRID",           (0, 0), (-1, -1), 0.3, RULE),
        ("LINEABOVE",      (0, 0), (-1, 0), 0, INK),
        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _section_card(title: str, flowables: list) -> list:
    """A titled section: teal label rule + content.

    Returns a flat list of flowables. The header label uses ``keepWithNext`` so a
    title never strands at the bottom of a page, but large content (multi-page
    tables) is *not* wrapped in ``KeepTogether`` — that would bump a whole table
    to the next page and leave the current one blank.
    """
    block = [
        Paragraph(_xml_escape(title.upper()), S["label_keep"]),
        HRFlowable(width=INNER_W, color=TEAL, thickness=1.2, spaceAfter=3 * mm),
    ]
    block.extend(flowables)
    block.append(Spacer(1, 4 * mm))
    return block


# Curated column sets for known list-of-dicts sections (wide schemas trimmed to
# the inspection-relevant columns so tables stay within the page width).
_CURATED_COLUMNS = {
    "processing_activities": (
        ["activity_name", "purpose", "legal_basis", "data_categories",
         "retention_period", "storage_location"],
        [3, 4, 3, 3, 2.5, 2.5],
    ),
    "cross_border_transfers_summary": (
        ["destination_country", "vendor", "data_transferred", "mechanism", "tia_in_place"],
        [2.5, 2.5, 3, 3, 1.5],
    ),
    "inventory": (
        ["category", "data_elements", "purpose", "legal_basis",
         "retention_period", "storage_location"],
        [2.5, 3, 3, 2.5, 2.5, 2.5],
    ),
    "vendors": (
        ["vendor_name", "role", "data_processed", "server_location",
         "dpa_status", "risk_level"],
        [3, 2, 3, 2.5, 2, 1.5],
    ),
    "gap_register": (
        ["vendor", "gap", "action", "deadline", "owner", "status"],
        [2.5, 3, 3, 2, 1.5, 1.5],
    ),
    "escalation_chain": (
        ["role", "action", "timeline"],
        [2.5, 4, 2.5],
    ),
    "review_schedule": (
        ["review_type", "frequency", "scope", "responsible", "next_due"],
        [2.5, 1.5, 4.5, 2, 1.5],
    ),
    "assessment_questions": (
        ["question", "options", "answer"],
        [3.5, 4.5, 1],
    ),
}


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


# ── LOGO ───────────────────────────────────────────────────────────────────

def _cover_logo(width_mm: float = 52) -> Image | None:
    """Centered Booppa logo flowable for the cover page (aspect preserved).

    Returns None when the asset is unavailable so the cover degrades gracefully.
    """
    if not LOGO_PATH:
        return None
    try:
        iw, ih = ImageReader(LOGO_PATH).getSize()
        w = width_mm * mm
        h = w * (ih / iw)
        img = Image(LOGO_PATH, width=w, height=h)
        img.hAlign = "CENTER"
        return img
    except Exception:
        return None


# ── HEADER / FOOTER ──────────────────────────────────────────────────────

def _header_footer(canvas, doc):
    canvas.saveState()
    # Header bar
    canvas.setFillColor(INK)
    canvas.rect(0, H - 14*mm, W, 14*mm, fill=1, stroke=0)
    # Logo (falls back to wordmark text if the asset is unavailable)
    logo_drawn = False
    if LOGO_PATH:
        try:
            logo_h = 8*mm
            # Explicit width required — canvas.drawImage silently drops the PNG
            # when given only height + preserveAspectRatio (logo is ~2.79:1).
            canvas.drawImage(
                LOGO_PATH, MARGIN, H - 14*mm + (14*mm - logo_h) / 2,
                width=logo_h * 2.79, height=logo_h, mask="auto",
            )
            logo_drawn = True
        except Exception:
            logo_drawn = False
    if not logo_drawn:
        canvas.setFillColor(TEAL)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(MARGIN, H - 8.5*mm, "BOOPPA INTELLIGENCE")
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(W - MARGIN, H - 8.5*mm, "PDPA Compliance Evidence Pack · BCEP-v1.1")
    # Footer
    canvas.setFillColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(MARGIN, 11*mm, W - MARGIN, 11*mm)
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
    elements.append(Spacer(1, 16*mm))

    # Centered brand logo
    logo = _cover_logo()
    if logo is not None:
        elements.append(logo)
        elements.append(Spacer(1, 10*mm))
    else:
        elements.append(Spacer(1, 6*mm))

    # Document badge
    elements.append(Paragraph(
        f"DOCUMENT {doc_meta['number']} OF 7 &nbsp;·&nbsp; {_xml_escape(doc_meta['ref'])}",
        S["label"]
    ))
    elements.append(Spacer(1, 3*mm))

    elements.append(Paragraph(_xml_escape(doc_meta["title"]), S["h1"]))
    elements.append(HRFlowable(width=INNER_W, color=TEAL, thickness=2, spaceAfter=6*mm))

    # Org block — assessed entity is the CUSTOMER (incl. their UEN).
    elements.append(_kv_table([
        ("Organisation",   pack["organisation"]),
        ("UEN",            pack.get("uen", "Not provided")),
        ("Pack ID",        pack["pack_id"]),
        ("Framework",      pack["framework"]),
        ("Generated",      pack["generated_at"][:10]),
        ("Next Review",    doc_meta.get("next_review", "12 months from effective date")),
        ("Status",         "ANCHORED · Polygon Amoy testnet"),
    ]))
    elements.append(Spacer(1, 9*mm))

    # Blockchain anchoring box
    anchor = pack.get("anchoring", {}).get(doc_meta["doc_type"], {})
    tx_hash   = anchor.get("tx_hash", "PENDING")
    doc_hash  = pack.get("hashes", {}).get(doc_meta["doc_type"], "")
    explorer_base = settings.active_polygon_explorer_url.rstrip("/")
    verify_url = anchor.get("verification_url") or (
        f"{explorer_base}/tx/{tx_hash}" if tx_hash and tx_hash != "PENDING" else "Pending"
    )

    def _chain_cell(text, color):
        return Paragraph(_xml_escape(text), ParagraphStyle(
            "chain", fontName="Courier", fontSize=7, leading=10, textColor=color))

    chain_rows = [
        ("SHA-256 Hash",    doc_hash or "Computing..."),
        ("Transaction",     tx_hash),
        ("Anchored On",     "Polygon Amoy testnet"),
        ("Anchor Time",     anchor.get("anchor_time_utc", "Pending")[:19].replace("T", " ") + " UTC"),
        ("Verify At",       verify_url),
    ]
    chain_data = [[_chain_cell(k, TEAL), _chain_cell(v, colors.white)] for k, v in chain_rows]
    chain_table = Table(chain_data, colWidths=[35*mm, INNER_W - 35*mm])
    chain_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), INK),
        ("VALIGN",      (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING",(0,0), (-1,-1), 8),
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
    warning_color = RED if is_critical else AMBER
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
            fontName="Helvetica-Bold", fontSize=8, textColor=colors.white, leading=11)),
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

# Keys handled by the cover/sign-off blocks or otherwise not part of the body.
_SKIP_KEYS = {
    "document_title", "organisation", "uen", "version",
    "draft_status", "legal_status", "_draft", "watermark",
}


def _render_value(key: str, value, depth: int = 0) -> list:
    """Render one (key, value) pair into flowables, choosing the best layout.

    - list of dicts  → records table (curated columns when known)
    - list of scalars → bullets
    - dict           → nested section card / kv table
    - scalar         → single kv row
    """
    title = _pretty(key)

    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            curated = _CURATED_COLUMNS.get(key)
            if curated:
                cols, weights = curated
                cols = [c for c in cols if any(c in it for it in value)] or list(value[0].keys())
                weights = weights[:len(cols)] if curated else None
            else:
                # derive columns from the union of keys, capped to keep it readable
                seen = []
                for it in value:
                    for k in it.keys():
                        if k not in seen:
                            seen.append(k)
                cols, weights = seen[:6], None
            table = _records_table(value, cols, weights)
            return _section_card(title, [table])
        else:
            bullets = [Paragraph("• " + _xml_escape(_stringify(v)), S["bullet"])
                       for v in value if v not in (None, "")]
            return _section_card(title, bullets or [Paragraph("—", S["body"])])

    if isinstance(value, dict):
        scalars, nested = [], []
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                nested.extend(_render_value(k, v, depth + 1))
            else:
                scalars.append((_pretty(k), v))
        inner = []
        if scalars:
            inner.append(_kv_table(scalars))
        inner.extend(nested)
        return _section_card(title, inner)

    # scalar at top level
    return [_kv_table([(title, value)])]


def _render_document_body(doc_data: dict) -> list:
    """Render the document JSON into a clean, sectioned body.

    Flat scalar fields are grouped into a single overview table; structured
    fields (dicts / lists) each become their own section card.
    """
    elements = []
    overview, structured = [], []
    for key, value in doc_data.items():
        if key in _SKIP_KEYS:
            continue
        if isinstance(value, (dict, list)):
            structured.append((key, value))
        else:
            overview.append((_pretty(key), value))

    if overview:
        elements.extend(_section_card("Document Overview", [_kv_table(overview)]))
    for key, value in structured:
        elements.extend(_render_value(key, value))
    return elements


def _render_inventory_table(inventory: list) -> list:
    """Data inventory as a compact, fully wrapping table (no cell overflow)."""
    cols, weights = _CURATED_COLUMNS["inventory"]
    cols = [c for c in cols if any(c in it for it in inventory)] or list(inventory[0].keys())
    return [_records_table(inventory, cols, weights[:len(cols)])]


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
    elements.append(Paragraph(_xml_escape(meta["title"]), S["h1"]))
    elements.append(Paragraph(_xml_escape(meta["ref"]), S["label"]))
    elements.append(HRFlowable(width=INNER_W, color=TEAL, thickness=1.5, spaceAfter=6*mm))

    # Render content
    if doc_type == "data_inventory" and "inventory" in doc_data:
        # Curated inventory table, then the rest of the document body.
        elements.extend(_section_card("Data Inventory", _render_inventory_table(doc_data["inventory"])))
        remaining = {k: v for k, v in doc_data.items() if k != "inventory"}
        elements.extend(_render_document_body(remaining))
    else:
        elements.extend(_render_document_body(doc_data))

    # Sign-off block
    elements.append(Spacer(1, 8*mm))
    elements.append(HRFlowable(width=INNER_W, color=RULE, thickness=0.5))
    elements.append(Spacer(1, 4*mm))

    approval = pack.get("approvals", {}).get(doc_type, {})
    if approval:
        sign_block = [_kv_table([
            ("Approved by",   approval.get("approver_email", "")),
            ("Approved at",   approval.get("approved_at", "")[:19].replace("T", " ") + " UTC"),
            ("Approval hash", approval.get("approval_hash", "")),
            ("Anchor TX",     approval.get("anchor", {}).get("tx_hash", "pending")),
        ], label_w=35*mm)]
        elements.extend(_section_card("Management Approval", sign_block))
    else:
        elements.append(Paragraph(
            "Awaiting management approval — approval will be anchored separately on the Booppa evidence chain.",
            S["caption"]
        ))

    doc.build(elements, onFirstPage=_header_footer, onLaterPages=_header_footer)

    if output_path:
        return output_path
    return buf.getvalue()


def build_evidence_pack_pdfs(pack: dict, output_dir: str = "/tmp") -> dict:
    """
    Build all 7 PDFs for an Evidence Pack.
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
