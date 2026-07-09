"""
vendor_proof_generator.py — Vendor Proof verification certificate (PDF).

Self-contained ReportLab generator (same pattern as ropa_generator.py). Produces
a 1-page certificate attesting the vendor's BOOPPA identity verification — ACRA
registration details (when matched), the verification level, the honest
procurement-readiness standing, and the blockchain anchor when present.

All buyer/registry-supplied strings are XML-escaped before being placed in a
ReportLab Paragraph.
"""
from app.services.pdf_styles import get_unified_styles
import logging
from io import BytesIO
from datetime import datetime, timezone

from app.services.tx_utils import is_real_onchain_tx

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
    entity_status: str | None = None,
    expires_on: str | None = None,
    notarization_credits: int = 0,
    sector_benchmark: dict | None = None,
) -> bytes:
    """Render the Vendor Proof certificate. Returns PDF bytes.

    `acra_data` (optional) may carry: entity_type, registration_date, industry,
    source, matched(bool). `score` is the honest compliance score (int) or a
    display string; when no PDPA scan exists this is "identity verified only".
    `entity_status` is the ACRA live status (e.g. "LIVE", "STRUCK OFF"); when it
    is anything other than live it is rendered as a prominent warning. `expires_on`
    is the certificate validity date.
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

    styles = get_unified_styles()
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
    # ACRA live entity status — flag non-live entities prominently (a struck-off
    # or ceased UEN must not read as a clean verification to a procurement officer).
    _status = (entity_status or "").strip()
    if _status:
        _is_live = "LIVE" in _status.upper() or _status.upper() == "REGISTERED"
        reg_rows.append((
            "Entity status",
            _xml_escape(_status) if _is_live
            else f'<font color="#b91c1c"><b>{_xml_escape(_status)} — verify entity is active before relying on this certificate</b></font>',
        ))
    story.append(Paragraph("Registration", sec_style))
    story.append(_kv(reg_rows))

    # Verification standing
    story.append(Paragraph("Verification standing", sec_style))
    _standing_rows = [
        ("Verification level", _xml_escape(verification_level)),
        ("Compliance score", score_display),
        ("Procurement readiness", _xml_escape(readiness_label)),
    ]
    if expires_on:
        _standing_rows.append((
            "Certificate valid until",
            f"{_xml_escape(expires_on)} — renews annually; trust status re-verified each year",
        ))
    story.append(_kv(_standing_rows))

    # Sector benchmark — turn the standalone Trust Score into a relative signal a
    # procurement officer can weigh. Rendered only when a real peer cohort exists
    # (see vendor_benchmark.compute_sector_benchmark); the basis (same-sector vs
    # all-vendors fallback) is stated honestly so the comparison isn't overclaimed.
    if isinstance(sector_benchmark, dict) and sector_benchmark.get("percentile") is not None:
        _pct = int(sector_benchmark["percentile"])
        _avg = sector_benchmark.get("sector_avg")
        _sector = _xml_escape(sector_benchmark.get("sector") or "peers")
        _n = sector_benchmark.get("peer_count")
        if sector_benchmark.get("basis") == "sector":
            _scope = f"in {_sector}"
        else:
            _scope = f"across all Booppa-scanned vendors (sector cohort for {_sector} too small to isolate)"
        _avg_txt = f" The {_sector} peer average is {int(_avg)}/100." if isinstance(_avg, (int, float)) else ""
        story.append(Paragraph("Sector benchmark", sec_style))
        story.append(Paragraph(
            f"This Trust Score is at or above <b>{_pct}%</b> of {_n} scored peers "
            f"{_scope}.{_avg_txt} Benchmarks are relative to Booppa's scanned-vendor "
            "population and move as more vendors are scored.",
            cell_value,
        ))

    # Notarization credits — mirror the Cover Sheet's redemption line so the
    # holder knows they carry a credit balance and how to redeem it. Rendered
    # from the holder's actual balance at issue (standalone Vendor Proof grants
    # none; the Vendor Trust Pack grants 2) — never claim a credit not held.
    if notarization_credits and notarization_credits > 0:
        _plural = "s" if notarization_credits != 1 else ""
        story.append(Paragraph("Notarization credits", sec_style))
        story.append(Paragraph(
            f"As of issue you hold <b>{notarization_credits} notarization "
            f"credit{_plural}</b>. Redeem by uploading a document at "
            "booppa.io/notarize to anchor its SHA-256 hash on-chain.",
            cell_value,
        ))

    # Blockchain anchor — render the "Blockchain anchor" block only for a real
    # on-chain tx, so a session id / sentinel is never shown as a transaction.
    if is_real_onchain_tx(tx_hash):
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
        story.append(Paragraph("Embeddable Trust Badge", sec_style))
        badge_snippet = f'&lt;a href="{_xml_escape(verify_url)}"&gt;&lt;img src="https://booppa.io/assets/trust-badge.svg" alt="Booppa Verified Vendor"/&gt;&lt;/a&gt;'
        story.append(Paragraph(
            f"Add this verified trust badge to your website's footer to signal compliance readiness to buyers:<br/>"
            f"<font name='Courier' size='8' color='#334155'>{badge_snippet}</font>",
            cell_value,
        ))
        
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
