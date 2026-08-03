"""Shared renderer for AI-generated compliance documents (CSP, MAS TRM).

Extracted from ``csp_doc_generator.generate_csp_document_pdf``. Two entry points:

- :func:`render_compliance_document` — markdown-ish narrative body → PDF bytes.
- :func:`render_register_table` — tabular registers (Outsourcing Register, VAPT
  log) that arrive as JSON rows and must render as a real table, not bullets.

Two properties worth preserving:

1. The draft banner is on the **first page**, above the body. CSP's original
   put a disclaimer paragraph at the end; a warning on the last page of a
   nine-page policy is not a warning. The layout mirrors the PDPA evidence
   pack (``evidence_pack/pdf_builder.py``), which got this right.
2. The signature block prints the SHA-256 of the **body text**, not of the PDF.
   Hashing the PDF here would be circular — the hash would have to be inside
   the bytes it describes. The PDF-bytes hash is what the caller anchors
   on-chain; the body hash is what lets a reader confirm the signed paper
   matches the text that was generated. Do not "fix" this to use the PDF hash.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.pdf_styles import get_unified_styles

# ── Draft status ───────────────────────────────────────────────────────────────

# Mirrors document_generator.DRAFT_DISCLAIMER. Persisted per document so a pack
# retains the exact wording it was delivered under, even if this constant moves.
TRM_DRAFT_DISCLAIMER: Dict[str, Any] = {
    "legal_status": "AI-GENERATED DRAFT",
    "evidentiary_value": (
        "This document has no evidentiary value until reviewed, corrected and signed "
        "by an authorised representative of the assessed entity."
    ),
    "verification_steps": (
        "1. Review every clause against your actual implemented controls. "
        "2. Replace every 'NOT YET EVIDENCED — [CUSTOMISE]' marker with the real control "
        "description, or record the gap honestly. "
        "3. Attach the supporting evidence referenced in each row. "
        "4. Have your authorised representative sign and date the signature block. "
        "5. Retain the signed copy; the signed PDF is what carries evidentiary weight."
    ),
    "anchor_note": (
        "SHA-256 hashes are anchored on the Booppa evidence chain (Polygon Amoy testnet). "
        "Anchoring proves the document existed in this form at that time; it is not a "
        "statement of compliance and is not an attestation by Booppa."
    ),
    "not_certification": (
        "Booppa Smart Care LLC does not certify, attest to, or warrant the regulatory "
        "compliance of the assessed entity. MAS Guidelines are guidance; MAS Notices are "
        "binding. Responsibility for compliance with either rests solely with the entity."
    ),
}

_CRITICAL_WARNING = (
    "This document is an AI-GENERATED DRAFT. Review, correct, and have your authorised "
    "representative sign it before it carries evidentiary value. It has NO evidentiary "
    "value until signed. Do not present it to MAS or any regulator in unsigned form."
)

_STANDARD_WARNING = (
    "This document is an AI-GENERATED DRAFT. It has NO evidentiary value until an "
    "authorised representative of the organisation reviews, corrects, and signs it. "
    "The signed PDF is then SHA-256 hashed and anchored on the Booppa evidence chain "
    "(Polygon Amoy testnet). Do not present this document in unsigned form."
)

_RED = "#b91c1c"
_AMBER = "#b45309"


def _escape(s: Any) -> str:
    """Escape text so ReportLab's Paragraph mini-XML doesn't misread ``&``/``<``."""
    from app.services.pdf_layout import xml_escape

    return xml_escape(s)


def _inline(s: str) -> str:
    """Escape, then re-introduce ``**bold**`` as the one permitted inline tag.

    Applied AFTER escaping, so a ``<b>`` typed by a user or emitted by the model
    stays inert text.
    """
    escaped = _escape(s)
    parts = escaped.split("**")
    rebuilt = []
    for i, part in enumerate(parts):
        # Odd indices are spans between paired markers. An unpaired trailing
        # marker stays literal rather than emitting a dangling tag.
        if i % 2 == 1 and i < len(parts) - 1:
            rebuilt.append(f"<b>{part}</b>")
        else:
            rebuilt.append(part)
    return "".join(rebuilt)


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    base = get_unified_styles()
    return {
        "h1": ParagraphStyle("CdH1", parent=base["Heading1"], fontSize=16, spaceAfter=10,
                             textColor=colors.HexColor("#0f172a")),
        "h2": ParagraphStyle("CdH2", parent=base["Heading2"], fontSize=13, spaceBefore=10,
                             spaceAfter=6, keepWithNext=1),
        "h3": ParagraphStyle("CdH3", parent=base["Heading3"], fontSize=11, spaceBefore=8,
                             spaceAfter=4, keepWithNext=1),
        "body": ParagraphStyle("CdBody", parent=base["BodyText"], fontSize=9.5, leading=14,
                               spaceAfter=6),
        "small": ParagraphStyle("CdSmall", parent=base["BodyText"], fontSize=7.5, leading=10,
                                textColor=colors.HexColor("#64748b")),
    }


def _draft_banner(critical: bool) -> list:
    """First-page warning band. RED for verification-critical documents."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    from app.services.pdf_layout import CONTENT_W

    label = "CRITICAL VERIFICATION REQUIRED" if critical else "VERIFICATION REQUIRED BEFORE EVIDENTIARY USE"
    text = _CRITICAL_WARNING if critical else _STANDARD_WARNING
    colour = colors.HexColor(_RED if critical else _AMBER)

    data = [[
        Paragraph("AI-GENERATED DRAFT<br/>" + _escape(label), ParagraphStyle(
            "cd_warn_label", fontName="Helvetica-Bold", fontSize=8,
            textColor=colors.white, leading=11)),
        Paragraph(_escape(text), ParagraphStyle(
            "cd_warn_body", fontName="Helvetica", fontSize=7.5,
            textColor=colors.white, leading=11)),
    ]]
    table = Table(data, colWidths=[50 * mm, CONTENT_W - 50 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colour),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("ROUNDEDCORNERS", [4]),
    ]))
    return [table, Spacer(1, 5 * mm)]


def _signature_block(meta: Dict[str, Any], body_sha256: str, styles) -> list:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    from app.services.pdf_layout import CONTENT_W

    rows: List[List[str]] = [
        ["Approved by (name)", "_" * 38],
        ["Designation", "_" * 38],
        ["Date of approval", "_" * 38],
        ["Signature", "_" * 38],
    ]
    if meta.get("legal_name"):
        rows.append(["Assessed entity", str(meta["legal_name"])])
    if meta.get("uen"):
        rows.append(["UEN", str(meta["uen"])])
    if meta.get("board_approval_ref"):
        rows.append(["Board approval reference", str(meta["board_approval_ref"])])
    if meta.get("schema_version") is not None:
        rows.append(["Document schema version", str(meta["schema_version"])])
    # Hash of the BODY TEXT, not the PDF — see module docstring.
    rows.append(["Body text SHA-256", body_sha256])

    data = [[Paragraph(_escape(k), styles["small"]), Paragraph(_escape(v), styles["small"])]
            for k, v in rows]
    table = Table(data, colWidths=[55 * mm, CONTENT_W - 55 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [Paragraph("Approval &amp; Signature", styles["h3"]), table]


def _body_flow(body: str, styles) -> list:
    """Markdown-ish body → flowables. ``#``/``##``/``###``, ``- ``/``* ``, blank lines."""
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.units import inch

    bullet = _bullet_style(styles)
    flow = []
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flow.append(Spacer(1, 0.06 * inch))
            continue
        if line.startswith("### "):
            flow.append(Paragraph(_escape(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flow.append(Paragraph(_escape(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flow.append(Paragraph(_escape(line[2:]), styles["h2"]))
        elif line.lstrip().startswith(("- ", "* ")):
            flow.append(Paragraph(_inline(line.lstrip()[2:]), bullet, bulletText="•"))
        else:
            flow.append(Paragraph(_inline(line), styles["body"]))
    return flow


def _bullet_style(styles):
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle("CdBullet", parent=styles["body"], leftIndent=16, bulletIndent=4)


def render_compliance_document(
    title: str,
    body: str,
    meta: Optional[Dict[str, Any]] = None,
    *,
    header_label: str = "COMPLIANCE DOCUMENT",
    disclaimer: str = "",
    draft_banner: bool = True,
    signature_block: bool = True,
    extra_flow: Optional[list] = None,
) -> Tuple[bytes, str]:
    """Render a generated compliance document to a PDF.

    Returns ``(pdf_bytes, sha256_hex)`` where the hash is over the PDF bytes —
    that is the value the caller anchors on-chain. The signature block separately
    prints the hash of ``body``.

    ``meta`` may carry ``legal_name``, ``uen``, ``board_approval_ref``,
    ``schema_version`` and ``verification_critical`` (drives the banner colour).
    ``extra_flow`` is appended after the body and before the signature block —
    used by :func:`render_register_table` to insert the table.
    """
    from reportlab.lib.units import inch
    from reportlab.platypus import CondPageBreak, Paragraph, Spacer

    from app.services import pdf_layout as _pl
    from app.services.pdf_layout import keep_together_safe

    meta = meta or {}
    styles = _styles()

    flow: list = []
    if draft_banner:
        flow.extend(_draft_banner(bool(meta.get("verification_critical"))))

    flow.append(Paragraph(_escape(title), styles["h1"]))
    sub = []
    if meta.get("legal_name"):
        sub.append(_escape(str(meta["legal_name"])))
    if meta.get("uen"):
        sub.append("UEN " + _escape(str(meta["uen"])))
    if meta.get("licence_label"):
        sub.append(_escape(str(meta["licence_label"])))
    sub.append("Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    flow.append(Paragraph(" · ".join(sub), styles["small"]))
    flow.append(Spacer(1, 0.2 * inch))

    flow.extend(_body_flow(body, styles))

    if extra_flow:
        flow.extend(extra_flow)

    body_sha = hashlib.sha256((body or "").encode("utf-8")).hexdigest()

    if signature_block:
        # Conditional: an unconditional PageBreak gave every document a final
        # page containing nothing but the block.
        flow.append(CondPageBreak(2.2 * inch))
        flow.extend(keep_together_safe(_signature_block(meta, body_sha, styles)))

    if disclaimer:
        flow.append(CondPageBreak(1.6 * inch))
        flow.extend(keep_together_safe([
            Paragraph("Legal Disclaimer", styles["h3"]),
            Paragraph(_escape(disclaimer), styles["small"]),
        ]))

    buf = BytesIO()
    doc = _pl.build_doc(buf, title=title, header_label=header_label,
                        branding=meta.get("branding") or None)
    doc.author = "Booppa Smart Care LLC"
    _pl.render(doc, flow)
    pdf_bytes = buf.getvalue()
    return pdf_bytes, hashlib.sha256(pdf_bytes).hexdigest()


def render_register_table(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    weights: Optional[Sequence[float]] = None,
) -> list:
    """Flowables for a register table (Outsourcing Register, VAPT log).

    ``columns`` is the authoritative column set — supplied by the caller, never
    inferred from ``rows``, so a model that invents or drops a field cannot
    change the shape of a prescribed register. A missing key renders as an empty
    cell, which is visibly a gap rather than a silently narrower table.
    """
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    from app.services.pdf_layout import CONTENT_W

    if not columns:
        return []

    base = get_unified_styles()
    head_style = ParagraphStyle("CdRegHead", parent=base["BodyText"], fontSize=6.8,
                                leading=8.5, fontName="Helvetica-Bold",
                                textColor=colors.white)
    cell_style = ParagraphStyle("CdRegCell", parent=base["BodyText"], fontSize=6.8,
                                leading=8.5)

    if weights and len(weights) == len(columns):
        total = float(sum(weights)) or 1.0
        widths = [CONTENT_W * (w / total) for w in weights]
    else:
        widths = [CONTENT_W / len(columns)] * len(columns)

    data = [[Paragraph(_escape(c), head_style) for c in columns]]
    for row in rows or []:
        data.append([Paragraph(_inline(str(row.get(c, "") or "")), cell_style) for c in columns])

    if len(data) == 1:
        data.append([Paragraph("No entries recorded.", cell_style)] +
                    [Paragraph("", cell_style) for _ in columns[1:]])

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [table]
