"""
vendor_proof_generator.py — Vendor Proof verification certificate (PDF).

Self-contained ReportLab generator (same pattern as ropa_generator.py). Produces
a 1-page certificate attesting the vendor's BOOPPA identity verification — ACRA
registration details (when matched), the verification level, the honest
procurement-readiness standing, and the blockchain anchor when present.

All buyer/registry-supplied strings are XML-escaped before being placed in a
ReportLab Paragraph.
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
    logger.warning("[VendorProof] ReportLab not installed — PDF generation disabled")


def _xml_escape(value) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def generate_vendor_proof_certificate(
    company_name: str,
    uen: str | None,
    acra_data: dict | None,
    score,
    verification_level: str = "BASIC",
    readiness_label: str = "Identity verified — compliance not yet assessed",
    verified_on: str | None = None,
    verify_url: str | None = None,
    tx_hash: str | None = None,
    network_name: str | None = None,
    explorer_url: str | None = None,
) -> bytes:
    """Render the Vendor Proof certificate. Returns PDF bytes.

    `acra_data` (optional) may carry: entity_type, registration_date, industry,
    source, matched(bool). `score` is the honest compliance score (int) or a
    display string; when no PDPA scan exists this is "identity verified only".
    """
    if not _REPORTLAB_OK:
        raise RuntimeError("ReportLab is required for Vendor Proof certificate generation")

    acra_data = acra_data or {}
    verified_on = verified_on or datetime.now(timezone.utc).strftime("%d %B %Y")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title=f"Vendor Proof — {company_name}",
    )

    styles = getSampleStyleSheet()
    h_style = ParagraphStyle(
        "h", parent=styles["Heading1"], fontSize=20,
        textColor=colors.HexColor("#0f172a"), spaceAfter=2,
    )
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"], fontSize=10,
        textColor=colors.HexColor("#64748b"), spaceAfter=16,
    )
    sec_style = ParagraphStyle(
        "sec", parent=styles["Heading2"], fontSize=12,
        textColor=colors.HexColor("#0f172a"), spaceBefore=14, spaceAfter=6,
        keepWithNext=1,
    )
    cell_label = ParagraphStyle(
        "cl", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#475569"), fontName="Helvetica-Bold",
    )
    cell_value = ParagraphStyle(
        "cv", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#0f172a"),
    )
    foot_style = ParagraphStyle(
        "foot", parent=styles["Normal"], fontSize=8,
        textColor=colors.HexColor("#94a3b8"), spaceBefore=16,
    )

    score_display = f"{score}/100" if isinstance(score, (int, float)) else _xml_escape(score)

    story: list = [
        Paragraph("Vendor Proof — Verification Certificate", h_style),
        Paragraph(
            f"{_xml_escape(company_name)}  ·  Verified on BOOPPA  ·  {verified_on}",
            sub_style,
        ),
    ]

    def _kv(rows):
        t = Table(
            [[Paragraph(k, cell_label), Paragraph(v, cell_value)] for k, v in rows],
            hAlign="LEFT", colWidths=[2.1 * inch, 4.9 * inch],
        )
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    # Entity / registration
    matched = bool(acra_data.get("matched"))
    reg_rows = [
        ("Legal entity", _xml_escape(company_name)),
        ("UEN", _xml_escape(uen) or "Not provided"),
        ("ACRA registry match", "Matched" if matched else "No registry match on file"),
    ]
    if matched:
        if acra_data.get("entity_type"):
            reg_rows.append(("Entity type", _xml_escape(acra_data.get("entity_type"))))
        if acra_data.get("registration_date"):
            reg_rows.append(("Registration date", _xml_escape(acra_data.get("registration_date"))))
        if acra_data.get("industry"):
            reg_rows.append(("Industry", _xml_escape(acra_data.get("industry"))))
    story.append(Paragraph("Registration", sec_style))
    story.append(_kv(reg_rows))

    # Verification standing
    story.append(Paragraph("Verification standing", sec_style))
    story.append(_kv([
        ("Verification level", _xml_escape(verification_level)),
        ("Compliance score", score_display),
        ("Procurement readiness", _xml_escape(readiness_label)),
    ]))

    # Blockchain anchor
    if tx_hash:
        anchor_rows = [
            ("Network", _xml_escape(network_name) or "Polygon"),
            ("Transaction", _xml_escape(tx_hash)),
        ]
        if explorer_url:
            link = f"{explorer_url.rstrip('/')}/tx/{_xml_escape(tx_hash)}"
            anchor_rows.append(("Verify", f'<a href="{link}">{_xml_escape(link)}</a>'))
        story.append(Paragraph("Blockchain anchor", sec_style))
        story.append(_kv(anchor_rows))

    if verify_url:
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(
            f'Live verification page: <a href="{_xml_escape(verify_url)}">{_xml_escape(verify_url)}</a>',
            cell_value,
        ))

    story.append(Paragraph(
        "What this attests: the identity and (where matched) ACRA registration of the entity "
        "above, verified on BOOPPA. It is not, by itself, a compliance endorsement — the "
        "compliance score reflects the entity's latest PDPA scan, if any.",
        foot_style,
    ))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
