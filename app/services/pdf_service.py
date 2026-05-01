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

import re
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
from app.core.company import (
    COMPANY_LEGAL_FOOTER, COMPANY_NAME, COMPANY_UEN,
    COMPANY_FRAMEWORK_VERSION, COMPANY_DPO_EMAIL,
)

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
    os.path.join(_HERE, "..", "..", "data", "logo.png"),
    "/app/data/logo.png",
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

    # ── Header band (white-label override) ──────────────────────────────────
    _branding = getattr(doc, "_branding", None)
    header_color = colors.HexColor(_branding["secondary_color"]) if _branding and _branding.get("secondary_color") else NAVY
    canvas.setFillColor(header_color)
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

    # Report-type label — right side of header (white-label override)
    label = getattr(doc, "_report_type_label", "AUDIT REPORT")
    accent_color = colors.HexColor(_branding["primary_color"]) if _branding and _branding.get("primary_color") else EMERALD
    canvas.setFillColor(accent_color)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.24 * inch, label)

    # ── Footer ───────────────────────────────────────────────────────────────
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)

    canvas.setFillColor(SLATE)
    pdpa_footer_lines = getattr(doc, "_pdpa_footer_lines", None)
    if pdpa_footer_lines:
        # PDPA: two-line italic disclaimer at 7pt
        canvas.setFont("Helvetica-Oblique", 7)
        y = FOOTER_H - 9
        for line in pdpa_footer_lines:
            canvas.drawString(MARGIN, y, line)
            y -= 9
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    else:
        canvas.setFont("Helvetica", 6.5)
        footer_str = (_branding.get("footer_text") or COMPANY_LEGAL_FOOTER) if _branding else COMPANY_LEGAL_FOOTER
        canvas.drawString(MARGIN, FOOTER_H - 9, footer_str)
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
                qr_target = f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{tx_hash}"
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
            Paragraph("HASH ALGORITHM", s["Label"]),
            Paragraph("SHA-256", s["Body"]),
            Spacer(1, 5),
            Paragraph("VERIFICATION URL", s["Label"]),
            Paragraph(
                '<a href="{href}"><font color="#10b981">{disp}</font></a>'.format(
                    href=qr_target,
                    disp=(
                        url_display[:46] + "<br/>" + url_display[46:]
                        if len(url_display) > 50
                        else url_display
                    ),
                ),
                s["Mono"],
            ),
            Spacer(1, 5),
            Paragraph("ANCHORED ON", s["Label"]),
            Paragraph(f"{settings.POLYGON_NETWORK_NAME}  ·  Immutable blockchain record", s["Body"]),
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

    # ── PDPA: Scope of Assessment ──────────────────────────────────────────────

    def _scope_of_assessment_table(self) -> Table:
        """Table listing each scanned element and whether it is In Scope or Out of Scope."""
        GREEN = colors.HexColor("#d1fae5")
        RED_BG = colors.HexColor("#fee2e2")
        RED_FG = colors.HexColor("#dc2626")
        GRFG   = colors.HexColor("#065f46")

        header = [
            Paragraph("ELEMENT ASSESSED", self._s["Label"]),
            Paragraph("DESCRIPTION", self._s["Label"]),
            Paragraph("SCOPE STATUS", self._s["Label"]),
        ]
        in_scope = [
            ("Cookie Consent Mechanism",
             "Analysis of consent banner implementation and pre-consent cookie behaviour"),
            ("Privacy Policy (PDPA §11/13)",
             "Detection of privacy policy link and content indicators on homepage"),
            ("Security HTTP Headers",
             "HTTP response header analysis for PDPA §24 Protection Obligation"),
            ("Cookie Attributes",
             "Secure, HttpOnly and SameSite flag inspection on set-cookie responses"),
            ("DNC Registry Reference",
             "Detection of marketing opt-out mechanism and DNC references"),
            ("Data Subject Rights Mechanism",
             "Presence of access, correction and withdrawal request pathways"),
            ("NRIC / FIN Collection Signals",
             "Keyword detection of regulated identity document collection"),
        ]
        out_scope = [
            ("Backend Systems & Internal Data Flows",
             "Server-side processing, databases, internal APIs"),
            ("Employee Data Handling",
             "HR data, payroll, staff records"),
            ("Third-Party Processor Agreements",
             "DPA agreements and sub-processor contracts"),
            ("Data Breach Notification Procedures",
             "Internal incident response and PDPC notification workflows"),
        ]

        rows: list = [header]
        in_style_rows: list[int] = []
        out_style_rows: list[int] = []

        for element, desc in in_scope:
            r = len(rows)
            rows.append([
                Paragraph(element, self._s["Body"]),
                Paragraph(desc, self._s["Body"]),
                Paragraph('<font color="#065f46"><b>✓ In Scope</b></font>', self._s["Body"]),
            ])
            in_style_rows.append(r)

        for element, desc in out_scope:
            r = len(rows)
            rows.append([
                Paragraph(element, self._s["Body"]),
                Paragraph(desc, self._s["Body"]),
                Paragraph('<font color="#dc2626"><b>✗ Out of Scope</b></font>', self._s["Body"]),
            ])
            out_style_rows.append(r)

        col_w = [CONTENT_W * 0.26, CONTENT_W * 0.52, CONTENT_W * 0.22]
        t = Table(rows, colWidths=col_w)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",  (0, 0), (-1, 0), WHITE),
            ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",   (0, 0), (-1, -1), 8),
            ("GRID",       (0, 0), (-1, -1), 0.5, BORDER),
            ("VALIGN",     (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for r in in_style_rows:
            style_cmds.append(("BACKGROUND", (2, r), (2, r), GREEN))
        for r in out_style_rows:
            style_cmds.append(("BACKGROUND", (2, r), (2, r), RED_BG))
        t.setStyle(TableStyle(style_cmds))
        return t

    # ── PDPA: Compliance Score by Dimension ────────────────────────────────────

    def _compliance_score_table(self, findings: list, scan_data: dict | None = None) -> Table:
        """Seven-dimension scored compliance table with overall score row.

        Scores are computed from actual scan data when available so that
        different sites produce meaningfully different numbers.  The scoring
        methodology is:
          • Base score per dimension (92-100) when no issue is detected.
          • Deductions applied per specific sub-check that failed.
          • Severity of the matching finding provides a floor.
        This means two sites with the same violation *type* but different
        underlying data will receive different scores.
        """
        s = self._s
        sd = scan_data or {}  # raw assessment / scan data dict

        def _has(keywords: list[str]) -> dict | None:
            for f in findings:
                text = " ".join([
                    (f.get("check_id") or ""),
                    (f.get("type") or ""),
                    (f.get("title") or ""),
                ]).lower()
                if any(k in text for k in keywords):
                    return f
            return None

        # ── Cookie Consent ────────────────────────────────────────────────
        cookie_finding = _has(["consent", "cookie_consent", "no_consent_banner", "tracking_cookie"])
        consent_mech = sd.get("consent_mechanism") or {}
        detected_providers = consent_mech.get("detected_providers") or []
        if cookie_finding is None:
            cookie_score = 96
            cookie_status = "Compliant"
            provider_list = ", ".join(detected_providers[:3]) if detected_providers else "compliant mechanism"
            cookie_note = f"Consent mechanism detected ({provider_list}); pre-consent cookies blocked."
        else:
            # Start at 40 (HIGH floor), add points for partial compliance
            cookie_score = 15
            if consent_mech.get("has_cookie_banner"):
                cookie_score += 25  # banner exists but flawed
            if consent_mech.get("policy_mentions_banner"):
                cookie_score += 10  # at least mentioned in policy
            cookie_status = "Non-Compliant" if cookie_score < 50 else "Partial"
            cookie_note = "Cookie consent mechanism absent or non-compliant — consent required before tracking."

        # ── Privacy Policy + DPO ──────────────────────────────────────────
        pp_finding = _has(["privacy_policy", "no_privacy_policy", "privacy policy", "dpo", "no_dpo_contact"])
        pp_data = sd.get("privacy_policy") or {}
        dpo_data = sd.get("dpo_compliance") or {}
        if pp_finding is None:
            pp_score = 97
            pp_status = "Compliant"
            pp_link = pp_data.get("link") or ""
            dpo_email = dpo_data.get("dpo_email") or ""
            pp_note = "Privacy policy linked"
            if pp_link:
                pp_note += f" at {pp_link}"
            if dpo_email:
                pp_note += f"; DPO contact: {dpo_email}"
            pp_note += "."
        else:
            pp_score = 30
            if pp_data.get("found"):
                pp_score += 25  # policy exists but DPO missing
                pp_note = "Privacy policy found but DPO contact not publicly disclosed on website."
            elif dpo_data.get("has_dpo"):
                pp_score += 15  # DPO found but no policy link
                pp_note = "DPO reference found but no privacy policy link on homepage."
            else:
                pp_note = "Neither privacy policy link nor DPO contact found on publicly accessible pages."
            pp_status = "Non-Compliant" if pp_score < 50 else "Partial"

        # ── Security Headers ──────────────────────────────────────────────
        sec_finding = _has(["hsts", "csp", "x_frame", "x_content_type", "referrer", "security_headers", "https"])
        sec_headers = sd.get("security_headers") or {}
        if sec_finding is None:
            sec_score = 94
            sec_status = "Compliant"
            sec_note = "Security headers (HSTS, CSP, X-Frame-Options, etc.) correctly configured."
        else:
            # Score based on how many of the 6 headers are present
            present = sum(1 for v in sec_headers.values() if v)
            total = max(len(sec_headers), 1)
            sec_score = int(15 + (present / total) * 75)  # range 15-90
            missing = [k.upper().replace("_", "-") for k, v in sec_headers.items() if not v]
            sec_note = f"Missing: {', '.join(missing[:4])}." if missing else "Headers partially configured."
            sec_status = "Non-Compliant" if sec_score < 50 else "Partial"

        # ── Cookie Attributes ─────────────────────────────────────────────
        attr_finding = _has(["cookie_secure", "secure flag", "httponly", "samesite"])
        if attr_finding is None:
            attr_score = 96
            attr_status = "Compliant"
            attr_note = "Cookies correctly use Secure and HttpOnly flags."
        else:
            sev = (attr_finding.get("severity") or "MEDIUM").upper()
            attr_score = {"CRITICAL": 10, "HIGH": 25, "MEDIUM": 55, "LOW": 75}.get(sev, 55)
            attr_note = (attr_finding.get("description") or attr_finding.get("title") or
                         "Cookie security attributes incomplete.")[:200]
            attr_status = "Non-Compliant" if attr_score < 50 else "Partial"

        # ── DNC Registry ──────────────────────────────────────────────────
        dnc_finding = _has(["dnc", "marketing", "do_not_call", "spam"])
        dnc_data = sd.get("dnc_mention") or {}
        if dnc_finding is None:
            dnc_score = 92
            dnc_status = "Compliant"
            dnc_note = "DNC opt-out mechanism referenced; marketing consent pathway present."
        else:
            dnc_score = 45
            if dnc_data.get("mentions_dnc"):
                dnc_score += 20  # mentioned but implementation issue
                dnc_note = "DNC referenced in content but opt-out mechanism not clearly implemented."
            else:
                dnc_note = "No DNC Registry reference or marketing opt-out mechanism detected."
            dnc_status = "Non-Compliant" if dnc_score < 50 else "Partial"

        # ── Data Subject Rights ───────────────────────────────────────────
        rights_finding = _has(["data_subject", "rights", "access_request", "correction", "withdrawal"])
        if rights_finding is None:
            rights_score = 91
            rights_status = "Compliant"
            rights_note = "Access, correction and withdrawal request pathways detected."
        else:
            sev = (rights_finding.get("severity") or "MEDIUM").upper()
            rights_score = {"CRITICAL": 10, "HIGH": 25, "MEDIUM": 55, "LOW": 75}.get(sev, 55)
            rights_note = "Data subject rights mechanism not detected — required under PDPA §21–22."
            rights_status = "Non-Compliant" if rights_score < 50 else "Partial"

        # ── NRIC / FIN ────────────────────────────────────────────────────
        nric_finding = _has(["nric", "fin", "nric_collection", "identity document"])
        if nric_finding is None:
            nric_score = 100
            nric_status = "Compliant"
            nric_note = "No NRIC/FIN collection points detected on publicly accessible pages."
        else:
            nric_evidence = sd.get("nric_evidence") or "NRIC/FIN keywords detected"
            nric_score = 5
            nric_note = f"Possible NRIC/FIN collection detected: {nric_evidence}."
            nric_status = "Non-Compliant"

        dimensions = [
            ("Cookie Consent Mechanism", cookie_score, cookie_status, cookie_note),
            ("Privacy Policy (PDPA §11/13)", pp_score, pp_status, pp_note),
            ("Security HTTP Headers", sec_score, sec_status, sec_note),
            ("Cookie Attributes", attr_score, attr_status, attr_note),
            ("DNC Registry Reference", dnc_score, dnc_status, dnc_note),
            ("Data Subject Rights Mechanism", rights_score, rights_status, rights_note),
            ("NRIC / FIN Collection Signals", nric_score, nric_status, nric_note),
        ]

        scores = [d[1] for d in dimensions]
        statuses = [d[2] for d in dimensions]
        overall = round(sum(scores) / len(scores))
        if all(s == "Compliant" for s in statuses):
            overall_status = "Compliant"
            overall_color = "#065f46"
            overall_bg = colors.HexColor("#d1fae5")
        elif any(s == "Non-Compliant" for s in statuses):
            overall_status = "Non-Compliant"
            overall_color = "#dc2626"
            overall_bg = colors.HexColor("#fee2e2")
        else:
            overall_status = "Partial"
            overall_color = "#92400e"
            overall_bg = colors.HexColor("#fef3c7")

        header = [
            Paragraph("DIMENSION", s["Label"]),
            Paragraph("SCORE", s["Label"]),
            Paragraph("STATUS", s["Label"]),
            Paragraph("NOTE", s["Label"]),
        ]
        rows: list = [header]
        for dim_name, dim_score, dim_status, dim_note in dimensions:
            if dim_status == "Compliant":
                status_html = f'<font color="#065f46"><b>{dim_status}</b></font>'
            elif dim_status == "Non-Compliant":
                status_html = f'<font color="#dc2626"><b>{dim_status}</b></font>'
            else:
                status_html = f'<font color="#92400e"><b>{dim_status}</b></font>'
            rows.append([
                Paragraph(dim_name, s["Body"]),
                Paragraph(f"<b>{dim_score}/100</b>", s["Body"]),
                Paragraph(status_html, s["Body"]),
                Paragraph(dim_note, s["Body"]),
            ])

        # Overall score row
        rows.append([
            Paragraph("<b>Overall Score</b>", s["Body"]),
            Paragraph(f'<font color="{overall_color}"><b>{overall}/100</b></font>', s["Body"]),
            Paragraph(f'<font color="{overall_color}"><b>{overall_status}</b></font>', s["Body"]),
            Paragraph("Aggregate across all assessed PDPA compliance dimensions.", s["Body"]),
        ])

        col_w = [CONTENT_W * 0.26, CONTENT_W * 0.12, CONTENT_W * 0.18, CONTENT_W * 0.44]
        t = Table(rows, colWidths=col_w)
        style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("GRID",          (0, 0), (-1, -1), 0.5, BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [WHITE, LIGHT_BG]),
            ("BACKGROUND",    (0, -1), (-1, -1), overall_bg),
            ("FONTNAME",      (0, -1), (-1, -1), "Helvetica-Bold"),
        ]
        t.setStyle(TableStyle(style_cmds))
        return t

    # ── PDPA: Assessment Conducted By ─────────────────────────────────────────

    def _assessment_conducted_by_section(self, report_data: dict) -> list:
        """Table identifying the assessing entity — required for legal standing."""
        s = self._s
        created_raw = report_data.get("created_at") or datetime.now(timezone.utc).isoformat()
        try:
            dt = datetime.fromisoformat(created_raw[:19]).replace(tzinfo=timezone.utc)
            date_display = dt.strftime("%d %B %Y  %H:%M UTC")
        except Exception:
            date_display = created_raw[:19] + " UTC"

        rows = [
            ("ASSESSING ENTITY",    COMPANY_NAME),
            ("UEN (SINGAPORE)",     COMPANY_UEN),
            ("FRAMEWORK VERSION",   COMPANY_FRAMEWORK_VERSION),
            ("ASSESSMENT DATE/TIME", date_display),
            ("ASSESSED ENTITY",     report_data.get("company_name") or "—"),
            ("ASSESSED URL",        report_data.get("vendor_url") or report_data.get("website_url") or report_data.get("url") or "—"),
            ("DPO CONTACT",         COMPANY_DPO_EMAIL),
        ]
        label_w = 1.7 * inch
        data = [
            [Paragraph(lbl, s["Label"]), Paragraph(str(val), s["Value"])]
            for lbl, val in rows
        ]
        t = Table(data, colWidths=[label_w, CONTENT_W - label_w])
        t.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_BG]),
            ("BOX",           (0, 0), (-1, -1), 0.5, BORDER),
            ("LINEAFTER",     (0, 0), (0, -1), 0.5, BORDER),
        ]))
        return [t, Spacer(1, 0.15 * inch)]

    # ── PDPA: How to Verify ───────────────────────────────────────────────────

    def _how_to_verify_block(self, report_data: dict) -> list:
        """4-step independent verification instructions (mandatory per legal brief)."""
        s = self._s
        audit_hash = report_data.get("audit_hash") or "—"
        tx_hash = report_data.get("tx_hash") or "—"
        steps = [
            (
                "Step 1 — Obtain the PDF from the assessed organisation.",
                "Request a copy of this certificate directly from the assessed entity.",
            ),
            (
                "Step 2 — Generate a SHA-256 hash of the PDF.",
                "macOS / Linux:  <b>shasum -a 256 filename.pdf</b><br/>"
                "Windows:        <b>CertUtil -hashfile filename.pdf SHA256</b>",
            ),
            (
                "Step 3 — Compare against the Evidence Hash on the certificate.",
                f"The output must exactly match: <font face='Courier'>{audit_hash}</font>",
            ),
            (
                f"Step 4 — Confirm the Transaction Hash on {settings.POLYGON_EXPLORER_URL}.",
                f"Search <font face='Courier'>{tx_hash}</font> on "
                f'<a href="{settings.POLYGON_EXPLORER_URL}"><font color="#10b981">{settings.POLYGON_EXPLORER_URL}</font></a>. '
                "The block timestamp proves the earliest possible existence date of this document. "
                "No login or account required.",
            ),
        ]
        items: list = []
        for title, detail in steps:
            items.append(Paragraph(f"<b>{title}</b>", s["Body"]))
            items.append(Paragraph(detail, s["Body"]))
            items.append(Spacer(1, 5))
        return items

    # ── PDPA: Compliance Strengths (no-violation case) ────────────────────────

    def _compliance_strengths_block(self, report_data: dict) -> list:
        """Positive evidence statement when no violations are detected."""
        s = self._s
        company = report_data.get("company_name") or "the assessed entity"
        structured = report_data.get("structured_report") or {}
        exec_sum = structured.get("executive_summary") or ""

        strengths = [
            (
                "Cookie Consent Implementation",
                f"{company} has implemented a compliant cookie consent mechanism. "
                "Non-essential cookies (analytics, advertising) are deferred until affirmative user consent "
                "is obtained, consistent with PDPA §13 Consent Obligation.",
            ),
            (
                "Privacy Policy Accessibility",
                "A privacy or data protection policy is linked from the homepage. "
                "This satisfies PDPA §11 Openness Obligation, ensuring users can access "
                "data handling information prior to providing personal data.",
            ),
            (
                "Transport Security",
                "The website enforces HTTPS across all pages, encrypting personal data "
                "in transit between users and the server, consistent with PDPA §24 "
                "Protection Obligation.",
            ),
            (
                "DNC Registry Alignment",
                "Marketing communications appear to reference or respect Do-Not-Call (DNC) "
                "Registry requirements under the PDPA Do Not Call Provisions.",
            ),
            (
                "Data Subject Rights Pathway",
                "An access and/or correction request pathway is present on the website, "
                "enabling individuals to exercise their rights under PDPA §21–22.",
            ),
        ]

        items: list = [
            Paragraph(
                f"The automated assessment of {company} found no PDPA violations across all "
                f"scanned dimensions. The following compliance strengths were identified:",
                s["Body"],
            ),
            Spacer(1, 8),
        ]

        for title, detail in strengths:
            items.append(Paragraph(
                f'<font color="#10b981"><b>✓ {title}</b></font>',
                s["Body"],
            ))
            items.append(Paragraph(detail, s["Body"]))
            items.append(Spacer(1, 5))

        if exec_sum:
            items.append(Spacer(1, 4))
            items.append(Paragraph("<b>AI Assessment Summary:</b>", s["Body"]))
            for para in [p.strip() for p in exec_sum.split("\n\n") if p.strip()][:2]:
                items.append(Paragraph(para.replace("\n", " "), s["Body"]))
                items.append(Spacer(1, 4))

        items.append(Spacer(1, 8))
        items.append(Paragraph(
            "No immediate remediation is required. We recommend scheduling a follow-up audit "
            "in 6 months to confirm continued compliance as your website evolves.",
            s["Body"],
        ))
        return items

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

            # Compute framework type early — gates several sections below
            framework_raw = (report_data.get("framework") or "").upper()
            is_pdpa = framework_raw in {"PDPA", "PDPA_QUICK_SCAN"}
            is_notarization = "NOTARIZATION" in framework_raw

            # Change 6(b/d): set PDPA footer disclaimer on doc object for _draw_page
            if is_pdpa:
                _disc = (
                    f"Automated compliance assessment by {COMPANY_NAME} · "
                    f"{COMPANY_FRAMEWORK_VERSION} · Results reflect publicly accessible website elements at assessment date."
                )
                _disc2 = (
                    "May be used as supporting evidence in procurement and regulatory contexts. "
                    f"Does not substitute for legal counsel. {COMPANY_NAME}, Singapore UEN: {COMPANY_UEN}."
                )
                doc._pdpa_footer_lines = [_disc, _disc2]

            # ── Cover ──────────────────────────────────────────────────────
            company = report_data.get("company_name") or "Vendor Report"
            framework = (report_data.get("framework") or "").replace("_", " ").title()

            story.append(Spacer(1, 0.25 * inch))

            # Logo on cover page (if available)
            if _LOGO_PATH and is_pdpa:
                try:
                    story.append(Image(_LOGO_PATH, width=1.2 * inch, height=0.4 * inch,
                                       hAlign="LEFT"))
                    story.append(Spacer(1, 0.1 * inch))
                except Exception:
                    pass

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

            # ── Proof metadata (generic / notarization only) ───────────────
            proof_header = report_data.get("proof_header")
            schema_version = report_data.get("schema_version")
            if not is_pdpa and (proof_header or schema_version):
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

            # ── Report details (generic / notarization only) ───────────────
            if not is_pdpa:
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
                        if is_pdpa:
                            # PDPA: inline screenshot without section header, smaller height
                            story.append(Image(img_buf, width=CONTENT_W, height=CONTENT_W * 0.45))
                        else:
                            story.append(self._section_header("Site Screenshot"))
                            story.append(Spacer(1, 6))
                            story.append(Image(img_buf, width=CONTENT_W, height=CONTENT_W * 0.55))
                        story.append(Spacer(1, 0.15 * inch))
                except Exception as e:
                    logger.warning(f"Screenshot render failed: {e}")

            # ── Key issues + PDPA action ───────────────────────────────────
            key_issues = report_data.get("key_issues") or []

            if is_notarization:
                story.extend(self._notarization_certificate_story(report_data))

            elif is_pdpa:
                # PDPA Quick Scan — Developer Brief Layout
                structured = report_data.get("structured_report") or {}
                findings = structured.get("detailed_findings") or []

                from reportlab.platypus import PageBreak as _PageBreak

                company_name = report_data.get("company_name") or "the organisation"
                scan_date_str = report_data.get("created_at", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                assessed_url = report_data.get("website_url") or report_data.get("url") or "—"

                # ── SITE INACCESSIBLE — short-circuit report ──────────────────
                if report_data.get("site_inaccessible"):
                    _reason = report_data.get("site_inaccessible_reason") or "Site could not be accessed."
                    _http = report_data.get("http_status") or "N/A"

                    story.append(self._section_header("SCAN COULD NOT BE COMPLETED"))
                    story.append(Spacer(1, 8))
                    story.append(Paragraph(
                        f'<font color="#dc2626"><b>SITE INACCESSIBLE — RESCAN REQUIRED</b></font>',
                        s["CoverSub"],
                    ))
                    story.append(Spacer(1, 10))
                    story.append(self._meta_table([
                        ("ASSESSED URL", assessed_url),
                        ("HTTP STATUS", str(_http)),
                        ("SCAN DATE", scan_date_str),
                    ]))
                    story.append(Spacer(1, 12))
                    story.append(Paragraph(
                        f"<b>Reason:</b> {_reason}",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 10))
                    story.append(Paragraph(
                        "No compliance findings have been generated because the scanner could "
                        "not access the target website's content. Producing findings without "
                        "real data would be misleading and legally contestable.",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 12))
                    story.append(self._section_header("Recommended Actions"))
                    story.append(Spacer(1, 6))
                    for step in [
                        "Verify the website URL is correct and the site is currently online.",
                        "If the site uses a Web Application Firewall (Cloudflare, Akamai, AWS WAF, etc.), "
                        "whitelist the Booppa scanner IP address or arrange access from a whitelisted network.",
                        "If the site is geo-restricted, arrange a scan from within the allowed region (Singapore).",
                        "Once access is confirmed, request a rescan from the Booppa dashboard.",
                    ]:
                        story.append(Paragraph(f"• {step}", s["Bullet"]))
                    story.append(Spacer(1, 12))
                    story.append(self._section_header("Assessment Conducted By"))
                    story.append(Spacer(1, 6))
                    story.extend(self._assessment_conducted_by_section(report_data))

                    # Skip all normal PDPA sections — jump to end
                    doc.build(story)
                    buffer.seek(0)
                    logger.info("PDF generated (site inaccessible report)")
                    return buffer.getvalue()

                # ── Section 1: Scope of Assessment (Change 1) ─────────────────
                story.append(self._section_header("1. Scope of Assessment"))
                story.append(Spacer(1, 6))
                story.append(Paragraph(
                    "This compliance pack is based on information provided by the company's authorised "
                    "representative and automated website assessment conducted by Booppa on the date indicated. "
                    "The table below lists each element assessed and its scope status.",
                    s["Body"],
                ))
                story.append(Spacer(1, 8))
                story.append(self._scope_of_assessment_table())
                story.append(Spacer(1, 0.15 * inch))

                # ── Section 2: Context & Purpose ──────────────────────────────
                story.append(self._section_header("2. Context & Purpose of This Document"))
                story.append(Spacer(1, 6))
                story.append(Paragraph(
                    f"This document summarises a PDPA Quick Scan compliance audit performed by Booppa on the "
                    f"{company_name} website, translated into English and enriched with developer implementation tasks. "
                    f"It is intended to be forwarded directly to the development team.",
                    s["Body"],
                ))
                story.append(Spacer(1, 4))
                story.append(Paragraph(
                    f"The audit was conducted on {scan_date_str} and anchored on the {settings.POLYGON_NETWORK_NAME} blockchain "
                    f"for evidentiary integrity.",
                    s["Body"],
                ))
                story.append(Spacer(1, 0.15 * inch))

                # ── Section 3: Audit Findings Summary ─────────────────────────
                story.append(self._section_header("3. Audit Findings Summary"))
                story.append(Spacer(1, 6))
                if not findings:
                    # Change 7: Compliance Strengths when no violations
                    story.extend(self._compliance_strengths_block(report_data))
                else:
                    has_critical = any(f.get("severity") == "CRITICAL" for f in findings)
                    story.append(Paragraph(
                        f"Booppa AI compliance audit identified "
                        f"{'CRITICAL ' if has_critical else ''}"
                        f"violation{'s' if len(findings) != 1 else ''} requiring immediate action. "
                        f"{len(findings)} issue{'s' if len(findings) != 1 else ''} found:",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 8))
                    for i, f in enumerate(findings, 1):
                        story.append(KeepTogether(self._finding_summary_block(i, f)))
                        story.append(Spacer(1, 8))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 4: Compliance Score by Dimension (Change 2) ───────
                story.append(self._section_header("4. Compliance Score by Dimension"))
                story.append(Spacer(1, 6))
                story.append(Paragraph(
                    "Each PDPA compliance dimension has been independently scored based on "
                    "the actual scan data collected for this specific website. Scores are "
                    "calculated from the number and severity of sub-checks within each "
                    "dimension. A numeric score is shown even where the result is fully "
                    "compliant — a documented score is more evidentially credible than "
                    "an undeclared pass.",
                    s["Body"],
                ))
                story.append(Spacer(1, 8))
                # Pass raw scan data so scores are computed from actual evidence
                _scan_data = report_data.get("scan_data") or report_data
                story.append(self._compliance_score_table(findings, scan_data=_scan_data))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 5: Developer Implementation Tasks ──────────────────
                story.append(_PageBreak())
                story.append(self._section_header("5. Developer Implementation Tasks"))
                story.append(Spacer(1, 6))
                if findings:
                    story.append(Paragraph(
                        "The following tasks are organised by priority and timeline. "
                        "Each task includes the acceptance criteria required to close the finding.",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 8))
                    for i, f in enumerate(findings, 1):
                        story.append(KeepTogether(self._task_block(i, f)))
                        story.append(Spacer(1, 8))
                else:
                    story.append(Paragraph(
                        "No remediation tasks required. No violations were detected during this scan.",
                        s["Body"],
                    ))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 6: Assessment Conducted By (Change 5) ─────────────
                story.append(self._section_header("6. Assessment Conducted By"))
                story.append(Spacer(1, 6))
                story.extend(self._assessment_conducted_by_section(report_data))

                # ── Section 7: Blockchain Evidence Anchoring (Changes 3 & 4) ──
                story.append(self._section_header("7. Blockchain Evidence Anchoring"))
                story.append(Spacer(1, 6))
                if findings:
                    story.append(Paragraph(
                        f"The following artifacts must be anchored on the {settings.POLYGON_NETWORK_NAME} blockchain to create "
                        "an immutable, court-admissible compliance trail:",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 6))
                    story.append(self._blockchain_anchoring_table(findings))
                    story.append(Spacer(1, 6))
                else:
                    story.append(Paragraph(
                        "As no violations were detected, the primary artifact to anchor is this audit report "
                        "itself, providing immutable proof of a clean compliance assessment on the audit date.",
                        s["Body"],
                    ))
                    story.append(Spacer(1, 6))
                # Blockchain detail table (includes HASH ALGORITHM — Change 3)
                story.extend(self._blockchain_block(report_data))
                # How to Verify — 4 steps (Change 4)
                story.append(Paragraph(
                    "<b>How to Verify This Certificate Independently</b>",
                    s["Body"],
                ))
                story.append(Spacer(1, 6))
                story.extend(self._how_to_verify_block(report_data))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 8: Important Limitations ──────────────────────────
                # Change 6(a): disclaimer removed from here — moved to footer
                story.append(self._section_header("8. Important Limitations of This Scan"))
                story.append(Spacer(1, 6))
                story.append(Paragraph(
                    "This Quick Scan has the following limitations — further audit may be needed for:",
                    s["Body"],
                ))
                story.append(Spacer(1, 4))
                for lim in [
                    "Data Protection Officer (DPO) appointment verification (mandatory for many organisations under PDPA)",
                    "Cross-border data transfer compliance (PDPA Part X — e.g. transfers to cloud providers outside Singapore)",
                    "Internal data handling workflows, retention policies, and deletion procedures",
                    "Third-party vendor / data processor agreements",
                    "Data breach notification procedures (mandatory 3-day notification to PDPC)",
                    "Completeness and legal sufficiency of the Privacy Policy beyond DNC references",
                    "Employee data handling training records",
                ]:
                    story.append(Paragraph(f"• {lim}", s["Bullet"]))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 9: Compliance Timeline Summary ─────────────────────
                story.append(self._section_header("9. Compliance Timeline Summary"))
                story.append(Spacer(1, 6))
                if findings:
                    story.append(self._timeline_summary_table(findings))
                else:
                    story.append(Paragraph(
                        "No compliance actions required at this time. Schedule a follow-up audit in 6 months.",
                        s["Body"],
                    ))
                story.append(Spacer(1, 0.1 * inch))

                # ── Section 10: Legal References ───────────────────────────────
                story.append(self._section_header("10. Legal References"))
                story.append(Spacer(1, 6))
                refs = (structured.get("legal_references") or []) or self._default_legal_references(findings)
                for ref in refs:
                    title = ref.get("title") if isinstance(ref, dict) else str(ref)
                    url = ref.get("url") if isinstance(ref, dict) else None
                    if url:
                        story.append(Paragraph(
                            f'• {title}: <a href="{url}"><font color="#10b981">{url}</font></a>',
                            s["Body"],
                        ))
                    else:
                        story.append(Paragraph(f"• {title}", s["Body"]))
                    story.append(Spacer(1, 3))

            else:
                # Standard Layout
                if key_issues:
                    story.append(self._section_header("Key Issues Found"))
                    story.append(Spacer(1, 6))
                    for issue in key_issues:
                        story.append(Paragraph(f"• {issue}", s["Bullet"]))
                    story.append(Spacer(1, 0.12 * inch))
                    story.append(self._section_header("Action Required"))
                    story.append(Spacer(1, 6))
                    story.extend(self._pdpa_warning_block(report_data))

            # ── Blockchain verification (generic reports only — PDPA & notarization have their own) ──
            if not is_notarization and not is_pdpa:
                story.append(self._section_header("Blockchain Verification"))
                story.append(Spacer(1, 6))
                story.extend(self._blockchain_block(report_data))

            # ── Structured report sections (generic reports only) ─────────
            structured = None
            if not is_notarization and not is_pdpa:
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

            if structured and not is_pdpa:
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

            elif not is_pdpa and not is_notarization:
                # AI narrative fallback (generic reports only)
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

            if not is_pdpa and not is_notarization:
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

    def _notarization_certificate_story(self, d: dict) -> list:
        """Full notarization certificate layout — self-sufficient legal artifact."""
        s = self._s
        items = []

        def mono(text): return Paragraph(str(text), s["Mono"])
        def body(text): return Paragraph(str(text), s["Body"])
        def label(text): return Paragraph(str(text), s["Label"])

        # ── 1. Document Descriptor ────────────────────────────────────────────
        descriptor = d.get("document_descriptor") or ""
        if descriptor:
            items.append(self._section_header("Document"))
            items.append(Spacer(1, 6))
            items.append(self._meta_table([
                ("DOCUMENT", descriptor),
                ("FILE NAME", d.get("original_filename") or "—"),
                ("FILE SIZE", f"{d.get('file_size'):,} bytes" if d.get("file_size") else "—"),
                ("MIME TYPE", d.get("mime_type") or "—"),
                ("SUBMITTED BY", d.get("company_name") or "—"),
            ]))
        else:
            items.append(self._section_header("Document"))
            items.append(Spacer(1, 6))
            items.append(self._meta_table([
                ("FILE NAME", d.get("original_filename") or "—"),
                ("FILE SIZE", f"{d.get('file_size'):,} bytes" if d.get("file_size") else "—"),
                ("MIME TYPE", d.get("mime_type") or "—"),
                ("SUBMITTED BY", d.get("company_name") or "—"),
            ]))
        items.append(Spacer(1, 0.15 * inch))

        # ── 2. Cryptographic Proof ─────────────────────────────────────────────
        items.append(self._section_header("Cryptographic Proof"))
        items.append(Spacer(1, 6))
        items.append(self._meta_table([
            ("ALGORITHM", d.get("hash_algorithm") or "SHA-256"),
            ("EVIDENCE HASH", d.get("audit_hash") or d.get("file_hash") or "—"),
        ]))
        items.append(Spacer(1, 0.15 * inch))

        # ── 3. Blockchain Record ───────────────────────────────────────────────
        items.append(self._section_header("Blockchain Record"))
        items.append(Spacer(1, 6))
        tx = d.get("tx_hash") or "—"
        poly_url = d.get("polygonscan_url") or (
            f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{tx}" if tx != "—" else "—"
        )
        items.append(self._meta_table([
            ("NETWORK", d.get("network") or settings.POLYGON_NETWORK_NAME),
            ("TRANSACTION HASH", tx),
            ("EXPLORER URL", poly_url),
            ("ANCHORED AT", d.get("created_at", "")[:19] or "—"),
        ]))
        items.append(Spacer(1, 6))

        # QR code + verify URL side-by-side (re-use _blockchain_block)
        items.extend(self._blockchain_block(d))
        items.append(Spacer(1, 0.1 * inch))

        # ── 4. How to Verify ──────────────────────────────────────────────────
        items.append(self._section_header("How to Verify This Certificate"))
        items.append(Spacer(1, 6))
        items.append(body(
            "Any third party can independently verify this certificate without accessing Booppa. "
            "Follow the steps below using standard tools."
        ))
        items.append(Spacer(1, 8))

        steps = [
            ("Step 1 — Obtain the original file",
             "Request the original document from the submitting party."),
            ("Step 2 — Generate a SHA-256 hash",
             "macOS / Linux:  shasum -a 256 filename\n"
             "Windows:        CertUtil -hashfile filename SHA256\n"
             "Online tool:    sha256file.com (upload the file locally in your browser)"),
            ("Step 3 — Compare hashes",
             f"The resulting hash must exactly match the Evidence Hash printed above:\n"
             f"{d.get('audit_hash') or d.get('file_hash') or '(see Cryptographic Proof section)'}"),
            ("Step 4 — Confirm the blockchain anchor",
             f"Search the Transaction Hash on {settings.POLYGON_EXPLORER_URL}. "
             f"The block timestamp proves the earliest possible existence date of this document. "
             f"No login or account required."),
        ]
        for title, detail in steps:
            items.append(Paragraph(f"<b>{title}</b>", s["Body"]))
            items.append(Paragraph(detail.replace("\n", "<br/>"), s["Body"]))
            items.append(Spacer(1, 6))
        items.append(Spacer(1, 0.1 * inch))

        # ── 5. Legal Disclaimer ───────────────────────────────────────────────
        items.append(self._section_header("Legal Disclaimer"))
        items.append(Spacer(1, 6))
        items.append(Paragraph(
            "This certificate provides cryptographic evidence of document existence at a specific date and time. "
            "It does NOT constitute legal notarization by a licensed notary public, nor does it validate the "
            "content or legality of the document. For legal matters, consult qualified legal counsel. "
            "Booppa is not affiliated with any government authority.",
            s["Disclaimer"]
        ))

        return items

    def _finding_summary_block(self, index: int, f: dict) -> list:
        """Section 2 card: FINDING N — Title [SEVERITY] with 4-row detail table."""
        title = f.get("title") or (f.get("type") or "Finding").replace("_", " ").title()
        severity = (f.get("severity") or "MEDIUM").upper()
        s = self._s

        header = Paragraph(
            f"FINDING {index} — {title}  {self._sev_badge(severity)}",
            s["FindHead"]
        )

        rows = []
        for label, value in [
            ("Violation",    f.get("description") or f.get("details") or ""),
            ("Legislation",  f.get("legislation_text") or "; ".join(f.get("legislation_references") or [])),
            ("Max Penalty",  f.get("max_penalty") or (f.get("penalty") or {}).get("amount") or "Up to S$1,000,000"),
            ("Evidence",     f.get("evidence") or "Automated scan detection"),
        ]:
            # strip AI template noise from violation text
            clean_val = value
            if label == "Violation" and "\n" in clean_val:
                # take just the first meaningful sentence
                first_line = clean_val.split("\n")[0].strip()
                clean_val = first_line if first_line else clean_val[:200]
            rows.append([
                Paragraph(label, s["Label"]),
                Paragraph(str(clean_val)[:400], s["Body"]),
            ])

        t = Table(rows, colWidths=[1.2 * inch, CONTENT_W - 1.2 * inch])
        t.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [WHITE, LIGHT_BG]),
            ("BOX",           (0, 0), (-1, -1), 0.5, BORDER),
        ]))
        return [header, Spacer(1, 4), t]

    def _task_block(self, index: int, f: dict) -> list:
        """Section 3 block: TASK N with Deadline, Owner, Requirements, Acceptance Criteria, Tools."""
        title = f.get("title") or (f.get("type") or "Finding").replace("_", " ").title()
        severity = (f.get("severity") or "MEDIUM").upper()
        priority_label = {"CRITICAL": "CRITICAL PRIORITY", "HIGH": "HIGH PRIORITY",
                          "MEDIUM": "MEDIUM PRIORITY", "LOW": "LOW PRIORITY"}.get(severity, "MEDIUM PRIORITY")
        s = self._s

        items = [
            Paragraph(f"TASK {index} — Implement {title}  {self._sev_badge(severity)}  [{priority_label}]", s["FindHead"]),
            Spacer(1, 6),
        ]

        deadline = f.get("deadline_short") or f.get("deadline") or "7 days"
        owner = f.get("owner") or "Development Team"
        items.append(Paragraph(f"<b>Deadline:</b> {deadline}", s["Body"]))
        items.append(Paragraph(f"<b>Owner:</b> {owner}", s["Body"]))
        items.append(Spacer(1, 4))

        requirements = f.get("requirements") or []
        if requirements:
            items.append(Paragraph("<b>Requirements:</b>", s["Body"]))
            for j, req in enumerate(requirements, 1):
                items.append(Paragraph(f"{j}. {req}", s["Bullet"]))
            items.append(Spacer(1, 4))

        acceptance = f.get("acceptance_criteria") or []
        if acceptance:
            items.append(Paragraph("<b>Acceptance Criteria:</b>", s["Body"]))
            for ac in acceptance:
                items.append(Paragraph(f"• {ac}", s["Bullet"]))
            items.append(Spacer(1, 4))

        tools = f.get("recommended_tools") or []
        if tools:
            items.append(Paragraph("<b>Recommended Tools / Libraries:</b>", s["Body"]))
            for tool in tools:
                items.append(Paragraph(f"• {tool}", s["Bullet"]))

        return items

    def _default_legal_references(self, findings: list) -> list:
        """Return default legal references based on finding types."""
        # Core references — always included for any PDPA report
        refs = [
            {"title": "Personal Data Protection Act 2012 (Singapore)",
             "url": "https://sso.agc.gov.sg/Act/PDPA2012"},
            {"title": "PDPA Section 11 — Openness Obligation",
             "url": "https://sso.agc.gov.sg/Act/PDPA2012#pr11-"},
            {"title": "PDPA Section 13 — Consent Obligation",
             "url": "https://sso.agc.gov.sg/Act/PDPA2012#pr13-"},
            {"title": "PDPA Section 24 — Protection Obligation",
             "url": "https://sso.agc.gov.sg/Act/PDPA2012#pr24-"},
            {"title": "PDPC Advisory Guidelines on Cookies (2021)",
             "url": "https://www.pdpc.gov.sg/-/media/Files/PDPC/PDF-Files/Advisory-Guidelines/AG-on-Cookies-2021.pdf"},
            {"title": "Guide to Enhanced Notice and Choice (2021)",
             "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2021/01/guide-to-enhanced-notice-and-choice"},
            {"title": "PDPC Advisory Guidelines on Key Concepts in the PDPA",
             "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2020/03/advisory-guidelines-on-key-concepts-in-the-pdpa"},
        ]

        # Contextual references — added when relevant findings are present
        types_and_ids = set()
        for f in findings:
            types_and_ids.add(f.get("type") or "")
            types_and_ids.add(f.get("check_id") or "")
            types_and_ids.add((f.get("title") or "").lower())

        combined = " ".join(types_and_ids)

        if "marketing" in combined or "dnc" in combined or "do_not_call" in combined:
            refs.append({"title": "PDPC DNC Registry Guidelines",
                         "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/guidelines/dnc-provisions"})
            refs.append({"title": "Spam Control Act (Cap. 311A)",
                         "url": "https://sso.agc.gov.sg/Act/SCA2007"})

        if "nric" in combined or "fin" in combined or "identity" in combined:
            refs.append({"title": "PDPC Advisory Guidelines on NRIC Numbers (2018)",
                         "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2018/01/advisory-guidelines-for-nric-numbers"})

        if "dpo" in combined or "data protection officer" in combined or "organizational" in combined:
            refs.append({"title": "PDPA Section 11(3) — DPO Designation & Public Disclosure",
                         "url": "https://sso.agc.gov.sg/Act/PDPA2012#pr11-"})

        if "breach" in combined or "notification" in combined:
            refs.append({"title": "PDPA Part VIA — Data Breach Notification",
                         "url": "https://sso.agc.gov.sg/Act/PDPA2012#PVIApr26A-"})

        return refs

    def _timeline_summary_table(self, findings: list) -> Table:
        """Create a Compliance Timeline Summary table as seen in the brief."""
        data = [["DEADLINE", "TASK", "ACTION REQUIRED", "PRIORITY"]]
        for f in findings:
            deadline = f.get("deadline_short") or f.get("deadline") or "7 days"
            title = f.get("title") or (f.get("type") or "").replace("_", " ").title()
            priority = f.get("severity", "MEDIUM")
            action = f"Deploy compliant {title}"

            data.append([
                Paragraph(deadline, self._s["Body"]),
                Paragraph(f"Implement {title}", self._s["Body"]),
                Paragraph(action, self._s["Body"]),
                Paragraph(f"{self._sev_badge(priority)}", self._s["Body"])
            ])
            
        t = Table(data, colWidths=[CONTENT_W * 0.15, CONTENT_W * 0.25, CONTENT_W * 0.40, CONTENT_W * 0.20])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_BG]),
        ]))
        return t

    def _blockchain_anchoring_table(self, findings: list) -> Table:
        """Create a table for Blockchain Evidence Anchoring artifacts."""
        data = [["ARTIFACT TO ANCHOR", "WHEN", "RESPONSIBLE"]]
        
        # Standard artifacts
        data.append([
            Paragraph("Consent banner deployment timestamp", self._s["Body"]),
            Paragraph("Within 48h of implementation", self._s["Body"]),
            Paragraph("Developer / DevOps", self._s["Body"])
        ])
        data.append([
            Paragraph("Privacy Policy update hash", self._s["Body"]),
            Paragraph("Within 7 days of implementation", self._s["Body"]),
            Paragraph("Developer / Legal", self._s["Body"])
        ])
        
        t = Table(data, colWidths=[CONTENT_W * 0.45, CONTENT_W * 0.30, CONTENT_W * 0.25])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_BG]),
        ]))
        return t

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
