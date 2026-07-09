"""
pdpa_declaration_generator.py — PDPA Level-2 self-declaration (PDF + schema).

Self-contained ReportLab generator (same pattern as ropa_generator.py). Provides
the intake schema, validation, and the anchored declaration PDF that elevates the
PDPA Quick Scan (Level 1) to PDPC Level 2.

  - PDPA_DECLARATION_SCHEMA       7-field intake form definition
  - PDPA_LEGAL_BASIS_OPTIONS      re-exported from ropa_generator (single source)
  - validate_pdpa_declaration     per-row required + length validation
  - generate_pdpa_declaration_pdf 1-section-per-activity PDF -> bytes
"""
from app.services.pdf_styles import get_unified_styles
import logging
from io import BytesIO
from datetime import datetime, timezone

from app.services.ropa_generator import PDPA_LEGAL_BASIS_OPTIONS  # single source of truth

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether,
    )
    from app.services.pdf_logo import draw_logo_header
    _REPORTLAB_OK = True
except ImportError:  # pragma: no cover
    _REPORTLAB_OK = False
    logger.warning("[PDPADeclaration] ReportLab not installed — PDF generation disabled")


# `max_length` mirrors the column limits in models_v12.PdpaSelfDeclaration.
PDPA_DECLARATION_SCHEMA = [
    {
        "key": "processing_purpose",
        "label": "Processing purpose",
        "help": "Why personal data is collected/used (e.g. payroll, customer support).",
        "max_length": 200,
        "type": "text",
    },
    {
        "key": "lawful_basis",
        "label": "Lawful basis",
        "help": "The PDPA basis relied on for this activity.",
        "max_length": 100,
        "type": "select",
    },
    {
        "key": "data_categories",
        "label": "Data categories",
        "help": "Types of personal data (e.g. name, NRIC, contact, financial).",
        "max_length": 500,
        "type": "text",
    },
    {
        "key": "data_subjects",
        "label": "Data subjects",
        "help": "Whose data (e.g. employees, customers, applicants).",
        "max_length": 200,
        "type": "text",
    },
    {
        "key": "recipients",
        "label": "Recipients / disclosures",
        "help": "Who the data is shared with (internal teams, processors, authorities).",
        "max_length": 400,
        "type": "text",
    },
    {
        "key": "retention_period",
        "label": "Retention period",
        "help": "How long data is kept and the disposal trigger.",
        "max_length": 300,
        "type": "text",
    },
    {
        "key": "safeguards",
        "label": "Protection safeguards",
        "help": "Security measures in place (access control, encryption, DPA with vendors).",
        "max_length": 500,
        "type": "text",
    },
]

_REQUIRED_KEYS = [f["key"] for f in PDPA_DECLARATION_SCHEMA]
_MAX_LENGTHS = {f["key"]: f["max_length"] for f in PDPA_DECLARATION_SCHEMA}
_LABELS = {f["key"]: f["label"] for f in PDPA_DECLARATION_SCHEMA}


def _xml_escape(value) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def validate_pdpa_declaration(activities) -> list[str]:
    """Validate a list of declaration dicts. Returns human-readable error strings
    (empty == valid)."""
    errors: list[str] = []
    if not isinstance(activities, list):
        return ["'activities' must be a list."]
    if not activities:
        return ["Add at least one processing activity."]
    if len(activities) > 20:
        errors.append("A maximum of 20 processing activities is allowed.")

    for idx, row in enumerate(activities, start=1):
        if not isinstance(row, dict):
            errors.append(f"Activity {idx}: must be an object.")
            continue
        for key in _REQUIRED_KEYS:
            raw = row.get(key)
            value = raw.strip() if isinstance(raw, str) else raw
            if not value:
                errors.append(f"Activity {idx}: '{_LABELS[key]}' is required.")
                continue
            if isinstance(value, str) and len(value) > _MAX_LENGTHS[key]:
                errors.append(
                    f"Activity {idx}: '{_LABELS[key]}' exceeds {_MAX_LENGTHS[key]} characters."
                )
        lawful = (row.get("lawful_basis") or "").strip()
        if lawful and lawful not in PDPA_LEGAL_BASIS_OPTIONS:
            errors.append(f"Activity {idx}: '{lawful}' is not a recognised PDPA legal basis.")

    return errors


def generate_pdpa_declaration_pdf(
    company_name: str,
    uen: str | None,
    rows,
    dpo_name: str | None = None,
    dpo_email: str | None = None,
) -> bytes:
    """Render the PDPA Level-2 self-declaration PDF. Returns PDF bytes."""
    if not _REPORTLAB_OK:
        raise RuntimeError("ReportLab is required for PDPA declaration generation")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"PDPA Level-2 Self-Declaration — {company_name}",
    )

    styles = get_unified_styles()
    h_style = ParagraphStyle("h", parent=styles["Heading1"], fontSize=18,
                             textColor=colors.HexColor("#0f172a"), spaceAfter=2)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9,
                               textColor=colors.HexColor("#64748b"), spaceAfter=14)
    act_style = ParagraphStyle("act", parent=styles["Heading2"], fontSize=12,
                               textColor=colors.HexColor("#0f172a"), spaceBefore=14,
                               spaceAfter=6, keepWithNext=1)
    cell_label = ParagraphStyle("cl", parent=styles["Normal"], fontSize=9,
                                textColor=colors.HexColor("#475569"), fontName="Helvetica-Bold")
    cell_value = ParagraphStyle("cv", parent=styles["Normal"], fontSize=9,
                                textColor=colors.HexColor("#0f172a"))
    foot_style = ParagraphStyle("foot", parent=styles["Normal"], fontSize=8,
                                textColor=colors.HexColor("#94a3b8"), spaceBefore=18)

    generated = datetime.now(timezone.utc).strftime("%d %b %Y")
    header_bits = [_xml_escape(company_name), f"UEN: {_xml_escape(uen) or 'Not provided'}",
                   f"Generated {generated}"]
    if dpo_name or dpo_email:
        header_bits.append("DPO: " + " · ".join(
            p for p in [_xml_escape(dpo_name), _xml_escape(dpo_email)] if p))

    story: list = [
        Paragraph("PDPA Level-2 Self-Declaration", h_style),
        Paragraph("  ·  ".join(header_bits), sub_style),
        Paragraph(
            "This is the organisation's self-declaration of its personal-data processing "
            "activities and accountability measures under the PDPA, complementing the "
            "automated PDPA Snapshot (Level 1).",
            sub_style,
        ),
    ]

    rows = rows or []
    for i, row in enumerate(rows, start=1):
        purpose = _xml_escape((row.get("processing_purpose") or "").strip()) or "—"
        block = [Paragraph(f"Activity {i} — {purpose}", act_style)]
        table_data = [
            [Paragraph(f["label"], cell_label),
             Paragraph(_xml_escape((row.get(f["key"]) or "").strip()) or "—", cell_value)]
            for f in PDPA_DECLARATION_SCHEMA
        ]
        t = Table(table_data, hAlign="LEFT", colWidths=[1.9 * inch, 5.3 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        block.append(t)
        story.append(KeepTogether(block))

    if not rows:
        story.append(Paragraph("No processing activities were declared.", cell_value))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(
        "Generated by Booppa — PDPA Level-2 self-declaration. Self-declared by the organisation "
        "named above; tamper-evident hash anchoring is applied at issuance.",
        foot_style,
    ))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
