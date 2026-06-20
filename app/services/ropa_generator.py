"""
ropa_generator.py — ROPA Lite (Record of Processing Activities)

PDPC Level 2 evidence for the Compliance Evidence Pack. Provides:

  - ROPA_INTAKE_SCHEMA          the 6-field intake form definition (frontend
                                renders from this, so labels live in one place)
  - PDPA_LEGAL_BASIS_OPTIONS    the allowed legal_basis values
  - validate_ropa_intake(rows)  per-row required + length validation
  - generate_ropa_lite_pdf(...) 1-page-per-section ReportLab PDF -> bytes

Self-contained (same pattern as competitor_signals_generator.py) so it has no
coupling to the large pdf_service module. All buyer-supplied strings are XML
escaped before being placed in a ReportLab Paragraph.
"""
import logging
from io import BytesIO
from datetime import datetime, timezone

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
    logger.warning("[ROPA] ReportLab not installed — PDF generation disabled")


# ── Intake definition ─────────────────────────────────────────────────────────
# `max_length` mirrors the column limits in ropa_models.py so validation is
# consistent across the form, the API, and the DB.
ROPA_INTAKE_SCHEMA = [
    {
        "key": "processing_purpose",
        "label": "Processing purpose",
        "help": "Why you collect/use this data (e.g. payroll, marketing, CCTV security).",
        "max_length": 200,
        "type": "text",
    },
    {
        "key": "data_categories",
        "label": "Data categories",
        "help": "Types of personal data held (e.g. name, NRIC, bank account, email).",
        "max_length": 500,
        "type": "text",
    },
    {
        "key": "data_subjects",
        "label": "Data subjects",
        "help": "Whose data this is (e.g. employees, customers, job applicants).",
        "max_length": 200,
        "type": "text",
    },
    {
        "key": "retention_period",
        "label": "Retention period",
        "help": "How long the data is kept and the disposal trigger (e.g. 7 years after termination).",
        "max_length": 300,
        "type": "text",
    },
    {
        "key": "cross_border_transfer",
        "label": "Cross-border transfer",
        "help": "Whether data leaves Singapore and where to (e.g. None / AWS ap-southeast-1 / vendor in EU).",
        "max_length": 400,
        "type": "text",
    },
    {
        "key": "legal_basis",
        "label": "Legal basis",
        "help": "The PDPA basis for processing this activity.",
        "max_length": 100,
        "type": "select",
    },
]

PDPA_LEGAL_BASIS_OPTIONS = [
    "Consent",
    "Deemed Consent",
    "Legitimate Interests",
    "Contractual Necessity",
    "Legal or Regulatory Obligation",
    "Vital Interests",
    "Business Improvement",
]

_REQUIRED_KEYS = [f["key"] for f in ROPA_INTAKE_SCHEMA]
_MAX_LENGTHS = {f["key"]: f["max_length"] for f in ROPA_INTAKE_SCHEMA}


def _xml_escape(value: str) -> str:
    """Escape &, <, > for ReportLab's Paragraph mini-XML parser."""
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def validate_ropa_intake(activities) -> list[str]:
    """
    Validate a list of ROPA activity dicts. Returns a list of human-readable
    error strings (empty list == valid). Used by both the draft-save and
    submit endpoints.
    """
    errors: list[str] = []

    if not isinstance(activities, list):
        return ["'activities' must be a list."]
    if not activities:
        return ["Add at least one processing activity."]
    if len(activities) > 15:
        errors.append("A maximum of 15 processing activities is allowed.")

    for idx, row in enumerate(activities, start=1):
        if not isinstance(row, dict):
            errors.append(f"Activity {idx}: must be an object.")
            continue
        for key in _REQUIRED_KEYS:
            raw = row.get(key)
            value = (raw or "").strip() if isinstance(raw, str) else raw
            if not value:
                label = next(f["label"] for f in ROPA_INTAKE_SCHEMA if f["key"] == key)
                errors.append(f"Activity {idx}: '{label}' is required.")
                continue
            if isinstance(value, str) and len(value) > _MAX_LENGTHS[key]:
                label = next(f["label"] for f in ROPA_INTAKE_SCHEMA if f["key"] == key)
                errors.append(
                    f"Activity {idx}: '{label}' exceeds {_MAX_LENGTHS[key]} characters."
                )
        legal_basis = (row.get("legal_basis") or "").strip()
        if legal_basis and legal_basis not in PDPA_LEGAL_BASIS_OPTIONS:
            errors.append(
                f"Activity {idx}: '{legal_basis}' is not a recognised PDPA legal basis."
            )

    return errors


def generate_ropa_lite_pdf(
    company_name: str,
    uen: str,
    rows,
    dpo_name: str | None = None,
    dpo_email: str | None = None,
) -> bytes:
    """
    Generate the ROPA Lite PDF. Returns PDF bytes.

    `rows` is a list of activity dicts (the 6 ROPA_INTAKE_SCHEMA keys). The DPO
    header line renders only if a name or email is supplied — ROPA can be
    submitted before the RFP kit (which is where DPO details are collected), in
    which case those are simply omitted.
    """
    if not _REPORTLAB_OK:
        raise RuntimeError("ReportLab is required for ROPA PDF generation")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"ROPA Lite — {company_name}",
    )

    styles = getSampleStyleSheet()
    h_style = ParagraphStyle(
        "h", parent=styles["Heading1"], fontSize=18,
        textColor=colors.HexColor("#0f172a"), spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#64748b"), spaceAfter=14,
    )
    act_style = ParagraphStyle(
        "act", parent=styles["Heading2"], fontSize=12,
        textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6,
        keepWithNext=1,
    )
    cell_label = ParagraphStyle(
        "cl", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#475569"), fontName="Helvetica-Bold",
    )
    cell_value = ParagraphStyle(
        "cv", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#0f172a"),
    )
    foot_style = ParagraphStyle(
        "foot", parent=styles["Normal"], fontSize=8,
        textColor=colors.HexColor("#94a3b8"), spaceBefore=18,
    )

    generated = datetime.now(timezone.utc).strftime("%d %b %Y")

    story: list = [
        Paragraph("Record of Processing Activities (ROPA Lite)", h_style),
    ]

    header_bits = [
        f"{_xml_escape(company_name)}",
        f"UEN: {_xml_escape(uen)}",
        f"Generated {generated}",
    ]
    if dpo_name or dpo_email:
        dpo_line = "DPO: " + " · ".join(
            p for p in [_xml_escape(dpo_name), _xml_escape(dpo_email)] if p
        )
        header_bits.append(dpo_line)
    story.append(Paragraph("  ·  ".join(header_bits), sub_style))

    story.append(Paragraph(
        "This record lists the personal-data processing activities declared by "
        "the organisation, in line with the PDPA accountability obligation.",
        sub_style,
    ))

    rows = rows or []
    for i, row in enumerate(rows, start=1):
        purpose = _xml_escape((row.get("processing_purpose") or "").strip()) or "—"
        block = [Paragraph(f"Activity {i} — {purpose}", act_style)]

        table_data = []
        for field in ROPA_INTAKE_SCHEMA:
            value = (row.get(field["key"]) or "").strip()
            table_data.append([
                Paragraph(field["label"], cell_label),
                Paragraph(_xml_escape(value) or "—", cell_value),
            ])

        t = Table(table_data, hAlign="LEFT", colWidths=[1.9 * inch, 5.3 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
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
        "Generated by Booppa — Compliance Evidence Pack. This ROPA Lite is a "
        "self-declared record provided by the organisation named above. "
        "Tamper-evident hash anchoring is applied at issuance.",
        foot_style,
    ))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
