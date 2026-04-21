"""
PDF generation service — Booppa brand layout
=============================================
Every page has:
  • Navy header band with logo + report-type label
  • Footer with URL and page number
Section headings use an emerald left-accent bar on a light background.
Metadata renders in a two-column label/value table.
Blockchain verification shows details + QR side-by-side.
Findings carry severity-colour badges (red/orange/amber/green).
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from io import BytesIO

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.core.config import settings
from app.core.company import COMPANY_LEGAL_FOOTER

logger = logging.getLogger(__name__)

# ── Brand palette ──────────────────────────────────────────────────────────────
NAVY = colors.HexColor("#0f172a")
EMERALD = colors.HexColor("#10b981")
SLATE = colors.HexColor("#64748b")
LIGHT_BG = colors.HexColor("#f8fafc")
BORDER = colors.HexColor("#e2e8f0")
TEXT_DARK = colors.HexColor("#1e293b")
WHITE = colors.white

SEVERITY_HEX = {
    "CRITICAL": "#ef4444",
    "HIGH": "#f97316",
    "MEDIUM": "#f59e0b",
    "LOW": "#10b981",
    "INFO": "#64748b",
}

# ── Logo resolution (tried at import time) ─────────────────────────────────────
_HERE = os.path.dirname(__file__)
_LOGO_CANDIDATES = [
    os.path.join(_HERE, "..", "..", "static", "logo.png"),
    "/app/static/logo.png",
]
_LOGO_PATH: str | None = None
for _c in _LOGO_CANDIDATES:
    _abs = os.path.abspath(_c)
    if os.path.exists(_abs):
        _LOGO_PATH = _abs
        break

# ── Page geometry ──────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
HEADER_H = 0.65 * inch
FOOTER_H = 0.40 * inch


# ── Per-page canvas callback ───────────────────────────────────────────────────


def _draw_page(canvas, doc):
    canvas.saveState()

    # ── Watermark (logo centred, diagonal, low opacity) ───────────────────────
    if _LOGO_PATH:
        try:
            canvas.saveState()
            canvas.setFillAlpha(0.06)
            wm_w = 5.5 * inch
            wm_h = wm_w * 0.35  # approximate logo aspect ratio
            canvas.translate(PAGE_W / 2, PAGE_H / 2)
            canvas.rotate(35)
            canvas.drawImage(
                _LOGO_PATH,
                -wm_w / 2,
                -wm_h / 2,
                width=wm_w,
                height=wm_h,
                preserveAspectRatio=True,
                mask="auto",
            )
            canvas.restoreState()
        except Exception:
            pass  # silently skip watermark if logo unavailable

    # ── Header band ──────────────────────────────────────────────────────────
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    logo_h = 0.35 * inch
    logo_y = PAGE_H - HEADER_H + (HEADER_H - logo_h) / 2

    if _LOGO_PATH:
        try:
            canvas.drawImage(
                _LOGO_PATH,
                MARGIN,
                logo_y,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            _draw_logo_text(canvas, logo_y)
    else:
        _draw_logo_text(canvas, logo_y)

    # Report-type label — right side of header
    label = getattr(doc, "_report_type_label", "AUDIT REPORT")
    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.24 * inch, label)

    # ── Footer ───────────────────────────────────────────────────────────────
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)

    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN, FOOTER_H - 9, COMPANY_LEGAL_FOOTER)
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")

    canvas.restoreState()


def _draw_logo_text(canvas, y: float):
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(MARGIN, y + 0.08 * inch, "BOOPPA")
    canvas.setFillColor(EMERALD)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(MARGIN + 56, y + 0.09 * inch, "·  Trust Intelligence")


# ── PDFService ─────────────────────────────────────────────────────────────────


class PDFService:
    """Generate branded Booppa PDF reports."""

    def __init__(self):
        self._s = self._build_styles()

    # ── Styles ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_styles() -> dict:
        def ps(name, **kw) -> ParagraphStyle:
            return ParagraphStyle(name, **kw)

        return {
            "CoverTitle": ps(
                "CoverTitle",
                fontSize=24,
                fontName="Helvetica-Bold",
                textColor=NAVY,
                spaceAfter=6,
                leading=28,
            ),
            "CoverSub": ps(
                "CoverSub",
                fontSize=12,
                fontName="Helvetica",
                textColor=SLATE,
                spaceAfter=4,
                leading=17,
            ),
            "SecHead": ps(
                "SecHead",
                fontSize=10,
                fontName="Helvetica-Bold",
                textColor=NAVY,
                spaceAfter=0,
                leading=13,
            ),
            "Body": ps(
                "Body",
                fontSize=9,
                fontName="Helvetica",
                textColor=TEXT_DARK,
                spaceAfter=4,
                leading=13,
            ),
            "Label": ps(
                "Label",
                fontSize=7,
                fontName="Helvetica-Bold",
                textColor=SLATE,
                spaceAfter=2,
                leading=10,
            ),
            "Value": ps(
                "Value",
                fontSize=9,
                fontName="Helvetica",
                textColor=TEXT_DARK,
                spaceAfter=2,
                leading=12,
            ),
            "Mono": ps(
                "Mono",
                fontSize=6.5,
                fontName="Courier",
                textColor=SLATE,
                spaceAfter=2,
                leading=9,
                wordWrap="LTR",
            ),
            "FindHead": ps(
                "FindHead",
                fontSize=9,
                fontName="Helvetica-Bold",
                textColor=NAVY,
                spaceAfter=3,
                leading=12,
            ),
            "Bullet": ps(
                "Bullet",
                fontSize=9,
                fontName="Helvetica",
                textColor=TEXT_DARK,
                leftIndent=10,
                spaceAfter=3,
                leading=13,
            ),
            "Disclaimer": ps(
                "Disclaimer",
                fontSize=7.5,
                fontName="Helvetica-Oblique",
                textColor=SLATE,
                spaceAfter=4,
                leading=11,
            ),
        }

    # ── Layout helpers ─────────────────────────────────────────────────────────

    def _section_header(self, title: str):
        """Emerald left-bar + label on light background."""
        cell = Paragraph(title.upper(), self._s["SecHead"])
        t = Table([[cell]], colWidths=[CONTENT_W])
        t.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                    ("LINEBEFORE", (0, 0), (0, -1), 3, EMERALD),
                    ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER),
                ]
            )
        )
        return t

    def _meta_table(self, rows: list[tuple[str, str]]) -> Table:
        """Alternating-row label / value table."""
        label_w = 1.5 * inch
        data = [
            [Paragraph(lbl, self._s["Label"]), Paragraph(str(val), self._s["Value"])]
            for lbl, val in rows
        ]
        t = Table(data, colWidths=[label_w, CONTENT_W - label_w])
        t.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_BG]),
                    ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER),
                ]
            )
        )
        return t

    def _cover_strip(self, report_data: dict) -> Table:
        """Three-cell meta strip below the cover title."""
        created = (
            report_data.get("created_at") or datetime.now(timezone.utc).isoformat()
        )
        try:
            dt = datetime.fromisoformat(created[:19])
            date_str = dt.strftime("%d %B %Y")
        except Exception:
            date_str = created[:10]

        cells = [
            Paragraph(
                f'<font color="#64748b"><b>DATE</b></font><br/>{date_str}',
                self._s["Body"],
            ),
            Paragraph(
                f'<font color="#64748b"><b>REPORT ID</b></font><br/>{report_data.get("report_id") or "—"}',
                self._s["Body"],
            ),
            Paragraph(
                f'<font color="#64748b"><b>STATUS</b></font><br/>{(report_data.get("status") or "Completed").title()}',
                self._s["Body"],
            ),
        ]
        t = Table([cells], colWidths=[CONTENT_W / 3] * 3)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEAFTER", (0, 0), (1, -1), 0.5, BORDER),
                    ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ]
            )
        )
        return t

    @staticmethod
    def _hr() -> HRFlowable:
        return HRFlowable(
            width="100%", thickness=0.5, color=BORDER, spaceAfter=8, spaceBefore=4
        )

    @staticmethod
    def _sev_badge(severity: str) -> str:
        hex_c = SEVERITY_HEX.get(severity.upper(), "#64748b")
        return f'<font color="{hex_c}"><b>[{severity.upper()}]</b></font>'

    # ── Blockchain section ─────────────────────────────────────────────────────

    def _blockchain_block(self, report_data: dict) -> list:
        tx_hash = report_data.get("tx_hash") or "—"
        audit_hash = report_data.get("audit_hash") or "—"
        payment_ok = bool(report_data.get("payment_confirmed"))
        verify_url = report_data.get("verify_url") or ""
        report_id = report_data.get("report_id") or ""

        if payment_ok and verify_url and audit_hash != "—":
            qr_target = verify_url
            url_display = verify_url
        elif not payment_ok or tx_hash == "—":
            qr_target = (
                report_data.get("pending_verification_url")
                or f"https://www.booppa.io/verify/pending?report_id={report_id}"
            )
            url_display = "Pending — scan to verify"
        else:
            try:
                qr_target = f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{tx_hash}"
            except Exception:
                qr_target = f"https://polygonscan.com/tx/{tx_hash}"
            url_display = qr_target

        # QR code
        qr_img = None
        try:
            qr = qrcode.QRCode(version=1, box_size=5, border=2)
            qr.add_data(qr_target)
            qr.make(fit=True)
            pil_img = qr.make_image(fill_color="#0f172a", back_color="white")
            buf = BytesIO()
            pil_img.save(buf, format="PNG")
            buf.seek(0)
            qr_img = Image(buf, width=1.5 * inch, height=1.5 * inch)
        except Exception as e:
            logger.warning(f"QR generation failed: {e}")

        s = self._s
        details_items = [
            Paragraph("TRANSACTION HASH", s["Label"]),
            Paragraph(tx_hash, s["Mono"]),
            Spacer(1, 5),
            Paragraph("EVIDENCE HASH", s["Label"]),
            Paragraph(audit_hash, s["Mono"]),
            Spacer(1, 5),
            Paragraph("VERIFICATION URL", s["Label"]),
            Paragraph(
                f'<a href="{qr_target}"><font color="#10b981">{url_display}</font></a>',
                s["Body"],
            ),
            Spacer(1, 5),
            Paragraph("ANCHORED ON", s["Label"]),
            Paragraph("Polygon PoS  ·  Immutable blockchain record", s["Body"]),
        ]

        qr_items = [
            Paragraph("SCAN TO VERIFY", s["Label"]),
            Spacer(1, 4),
            qr_img if qr_img else Paragraph("QR unavailable", s["Body"]),
        ]

        detail_w = CONTENT_W - 1.8 * inch - 6
        row = [[details_items, qr_items]]
        t = Table(row, colWidths=[detail_w, 1.8 * inch])
        t.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (1, 0), (1, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                    ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                    ("LINEAFTER", (0, 0), (0, -1), 0.5, BORDER),
                ]
            )
        )
        return [t, Spacer(1, 0.15 * inch)]

    # ── PDPA warning ───────────────────────────────────────────────────────────

    def _pdpa_warning_block(self, report_data: dict) -> list:
        prefill = report_data.get("contact_email") or ""
        host = getattr(settings, "APP_HOST", "0.0.0.0")
        if host in ("0.0.0.0", ""):
            host = "localhost"
        default_base = f"http://{host}:{getattr(settings, 'APP_PORT', '8000')}"
        base = (
            report_data.get("base_url")
            or os.environ.get("BACKEND_BASE_URL")
            or default_base
        )
        s = self._s

        warning = (
            "Under the updated PDPA, the PDPC may impose penalties of up to S$1 million or 10% "
            "of annual Singapore turnover. Compliance gaps are also monitored by competitors — "
            "a third-party report could trigger an immediate investigation and reputational damage."
        )

        products = [
            (
                "PDPA Quick Scan",
                "S$69",
                f"{base}/api/stripe/checkout?product=pdpa_quick_scan&prefill_email={prefill}",
            ),
            (
                "PDPA Essential",
                "S$299 / mo",
                f"{base}/api/stripe/checkout?product=pdpa_basic&prefill_email={prefill}",
            ),
            (
                "Standard Suite",
                "S$1,299 / mo",
                f"{base}/api/stripe/checkout?product=compliance_standard&prefill_email={prefill}",
            ),
            (
                "Pro Suite",
                "S$1,999 / mo",
                f"{base}/api/stripe/checkout?product=compliance_pro&prefill_email={prefill}",
            ),
        ]
        rows = [
            [
                Paragraph(
                    f'<a href="{url}"><font color="#10b981"><b>{name}</b></font></a>',
                    s["Body"],
                ),
                Paragraph(f"<b>{price}</b>", s["Body"]),
            ]
            for name, price, url in products
        ]
        pt = Table(rows, colWidths=[CONTENT_W * 0.65, CONTENT_W * 0.35])
        pt.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_BG]),
                    ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
                ]
            )
        )

        return [
            Paragraph(warning, s["Body"]),
            Spacer(1, 8),
            pt,
            Spacer(1, 0.1 * inch),
        ]

    # ── Main entry point ───────────────────────────────────────────────────────

    def generate_pdf(self, report_data: dict) -> bytes:
        try:
            buffer = BytesIO()
            s = self._s

            doc = BaseDocTemplate(
                buffer,
                pagesize=A4,
                leftMargin=MARGIN,
                rightMargin=MARGIN,
                topMargin=HEADER_H + 0.35 * inch,
                bottomMargin=FOOTER_H + 0.35 * inch,
            )
            doc._report_type_label = (
                (report_data.get("framework") or "AUDIT REPORT")
                .upper()
                .replace("_", " ")
            )
            frame = Frame(
                doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main"
            )
            doc.addPageTemplates(
                [PageTemplate(id="main", frames=[frame], onPage=_draw_page)]
            )

            story = []

            # ── Cover ──────────────────────────────────────────────────────
            company = report_data.get("company_name") or "Vendor Report"
            framework = (report_data.get("framework") or "").replace("_", " ").title()

            story.append(Spacer(1, 0.25 * inch))
            story.append(Paragraph(company, s["CoverTitle"]))
            story.append(
                Paragraph(framework or "Compliance Audit Report", s["CoverSub"])
            )
            story.append(Spacer(1, 0.1 * inch))
            story.append(
                HRFlowable(width="100%", thickness=2, color=EMERALD, spaceAfter=10)
            )
            story.append(self._cover_strip(report_data))
            story.append(Spacer(1, 0.3 * inch))

            # ── Proof metadata ─────────────────────────────────────────────
            proof_header = report_data.get("proof_header")
            schema_version = report_data.get("schema_version")
            if proof_header or schema_version:
                rows = []
                if proof_header:
                    rows.append(("FORMAT", proof_header))
                if schema_version:
                    rows.append(("SCHEMA VERSION", schema_version))
                story.append(
                    KeepTogether(
                        [
                            self._section_header("Proof Metadata"),
                            Spacer(1, 6),
                            self._meta_table(rows),
                            Spacer(1, 0.15 * inch),
                        ]
                    )
                )

            # ── Report details ─────────────────────────────────────────────
            created_raw = (
                report_data.get("created_at") or datetime.now(timezone.utc).isoformat()
            )
            story.append(
                KeepTogether(
                    [
                        self._section_header("Report Details"),
                        Spacer(1, 6),
                        self._meta_table(
                            [
                                ("REPORT ID", report_data.get("report_id") or "—"),
                                ("FRAMEWORK", framework or "—"),
                                ("COMPANY", company),
                                ("GENERATED", created_raw[:19]),
                                (
                                    "STATUS",
                                    (report_data.get("status") or "Completed").title(),
                                ),
                            ]
                        ),
                        Spacer(1, 0.2 * inch),
                    ]
                )
            )

            # ── Site screenshot ────────────────────────────────────────────
            ss = report_data.get("site_screenshot")
            if ss:
                try:
                    if isinstance(ss, str):
                        img_data = base64.b64decode(ss)
                    elif isinstance(ss, bytes):
                        img_data = ss
                    elif hasattr(ss, "read"):
                        img_data = ss.read()
                    else:
                        img_data = None
                    if img_data:
                        img_buf = BytesIO(img_data)
                        story.append(self._section_header("Site Screenshot"))
                        story.append(Spacer(1, 6))
                        story.append(
                            Image(img_buf, width=CONTENT_W, height=CONTENT_W * 0.55)
                        )
                        story.append(Spacer(1, 0.15 * inch))
                except Exception as e:
                    logger.warning(f"Screenshot render failed: {e}")

            # ── Key issues + PDPA action ───────────────────────────────────
            key_issues = report_data.get("key_issues") or []
            if key_issues:
                story.append(self._section_header("Key Issues Found"))
                story.append(Spacer(1, 6))
                for issue in key_issues:
                    story.append(Paragraph(f"• {issue}", s["Bullet"]))
                story.append(Spacer(1, 0.12 * inch))
                story.append(self._section_header("Action Required"))
                story.append(Spacer(1, 6))
                story.extend(self._pdpa_warning_block(report_data))

            # ── Blockchain verification ────────────────────────────────────
            story.append(self._section_header("Blockchain Verification"))
            story.append(Spacer(1, 6))
            story.extend(self._blockchain_block(report_data))

            # ── Structured report sections ─────────────────────────────────
            structured = None
            if isinstance(report_data.get("structured_report"), dict):
                structured = report_data["structured_report"]
            elif any(
                k in report_data
                for k in (
                    "executive_summary",
                    "detailed_findings",
                    "recommendations",
                    "legal_references",
                )
            ):
                structured = report_data

            if structured:
                # Executive summary
                exec_sum = structured.get("executive_summary") or ""
                if exec_sum:
                    story.append(self._section_header("Executive Summary"))
                    story.append(Spacer(1, 6))
                    for para in [
                        p.strip() for p in exec_sum.split("\n\n") if p.strip()
                    ]:
                        story.append(Paragraph(para.replace("\n", " "), s["Body"]))
                        story.append(Spacer(1, 4))
                    story.append(Spacer(1, 0.1 * inch))

                # Detailed findings
                findings = structured.get("detailed_findings") or []
                if findings:
                    story.append(self._section_header("Detailed Findings"))
                    story.append(Spacer(1, 6))
                    for i, f in enumerate(findings, 1):
                        f_type = (f.get("type") or "Finding").replace("_", " ").title()
                        severity = (f.get("severity") or "MEDIUM").upper()
                        desc = (
                            f.get("description")
                            or f.get("details")
                            or "No description."
                        )
                        evidence = f.get("evidence") or ""
                        penalty = (
                            (f.get("penalty") or {}).get("amount")
                            if isinstance(f.get("penalty"), dict)
                            else None
                        )

                        block = [
                            Paragraph(
                                f"{i}. {f_type}  {self._sev_badge(severity)}",
                                s["FindHead"],
                            ),
                            Paragraph(desc.replace("\n", " "), s["Body"]),
                        ]
                        if evidence:
                            block.append(
                                Paragraph(f"<i>Evidence: {evidence}</i>", s["Body"])
                            )
                        if penalty:
                            block.append(
                                Paragraph(
                                    f"<b>Potential penalty:</b> {penalty}", s["Body"]
                                )
                            )
                        block.append(Spacer(1, 6))
                        story.append(KeepTogether(block))
                    story.append(Spacer(1, 0.1 * inch))

                # Recommendations
                recs = structured.get("recommendations") or []
                if recs:
                    story.append(self._section_header("Recommendations"))
                    story.append(Spacer(1, 6))
                    for i, r in enumerate(recs, 1):
                        vtype = (
                            (r.get("violation_type") or "").replace("_", " ").title()
                        )
                        actions = r.get("actions") or []
                        tl = r.get("timeline") or ""
                        block = [
                            Paragraph(
                                f"{i}. {vtype}  {self._sev_badge(r.get('severity') or 'MEDIUM')}",
                                s["FindHead"],
                            ),
                        ]
                        for a in actions:
                            block.append(Paragraph(f"• {a}", s["Bullet"]))
                        if tl:
                            block.append(Paragraph(f"<b>Timeline:</b> {tl}", s["Body"]))
                        block.append(Spacer(1, 6))
                        story.append(KeepTogether(block))
                    story.append(Spacer(1, 0.1 * inch))

                # Legal references
                refs = structured.get("legal_references") or []
                if refs:
                    story.append(self._section_header("Legal References"))
                    story.append(Spacer(1, 6))
                    for ref in refs:
                        title = ref.get("title") if isinstance(ref, dict) else str(ref)
                        url = ref.get("url") if isinstance(ref, dict) else None
                        if url:
                            story.append(
                                Paragraph(
                                    f'• {title}: <a href="{url}"><font color="#10b981">{url}</font></a>',
                                    s["Body"],
                                )
                            )
                        else:
                            story.append(Paragraph(f"• {title}", s["Body"]))
                    story.append(Spacer(1, 0.1 * inch))

            else:
                # AI narrative fallback
                narrative = report_data.get("ai_narrative") or ""
                if narrative:
                    story.append(self._section_header("AI Analysis"))
                    story.append(Spacer(1, 6))
                    for para in [
                        p.strip() for p in narrative.split("\n\n") if p.strip()
                    ]:
                        story.append(Paragraph(para.replace("\n", " "), s["Body"]))
                        story.append(Spacer(1, 4))
                    story.append(Spacer(1, 0.1 * inch))

            # ── Disclaimer ─────────────────────────────────────────────────
            story.append(Spacer(1, 0.1 * inch))
            story.append(self._section_header("Disclaimer"))
            story.append(Spacer(1, 6))
            story.append(
                Paragraph(
                    "This report is provided for informational purposes only and does not constitute "
                    "legal advice, certification, or regulatory approval. Booppa does not certify "
                    "vendors, issue regulatory determinations, or publish public vendor scoring. "
                    "Organizations should consult qualified professionals for compliance decisions "
                    "and regulatory engagement.",
                    s["Disclaimer"],
                )
            )

            doc.build(story)
            buffer.seek(0)
            logger.info("PDF generated successfully")
            return buffer.getvalue()

        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            raise

    # ── Legacy compatibility (called by rfp_express_builder indirectly) ────────

    def _create_proof_metadata(self, report_data: dict) -> list:
        rows = []
        if report_data.get("proof_header"):
            rows.append(("FORMAT", report_data["proof_header"]))
        if report_data.get("schema_version"):
            rows.append(("SCHEMA VERSION", report_data["schema_version"]))
        if report_data.get("verify_url"):
            rows.append(("VERIFY URL", report_data["verify_url"]))
        return [self._meta_table(rows)] if rows else []

    def _create_detail_paragraphs(self, report_data: dict) -> list:
        return [
            self._meta_table(
                [
                    ("REPORT ID", report_data.get("report_id") or "N/A"),
                    ("FRAMEWORK", report_data.get("framework") or "N/A"),
                    ("COMPANY", report_data.get("company_name") or "N/A"),
                    (
                        "GENERATED",
                        (
                            report_data.get("created_at")
                            or datetime.now(timezone.utc).isoformat()
                        )[:19],
                    ),
                    ("STATUS", report_data.get("status") or "completed"),
                ]
            )
        ]

    def _create_mandatory_pdpa_warning(self, report_data: dict) -> list:
        return [
            self._section_header("Action Required"),
            Spacer(1, 6),
            *self._pdpa_warning_block(report_data),
        ]

    def _create_blockchain_section(self, report_data: dict) -> list:
        return self._blockchain_block(report_data)

    def _create_pdpa_disclaimer(self) -> list:
        return [
            Spacer(1, 0.15 * inch),
            self._section_header("Disclaimer"),
            Spacer(1, 6),
            Paragraph(
                "This report is provided for informational purposes only and does not constitute "
                "legal advice, certification, or regulatory approval.",
                self._s["Disclaimer"],
            ),
        ]
