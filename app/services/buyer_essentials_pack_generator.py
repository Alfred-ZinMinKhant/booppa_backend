"""
Buyer Essentials — Welcome Pack Generator
=========================================
A branded ReportLab PDF emailed to a buyer subscriber on their first cycle
(subscription activation). Unlike the Procurement Intelligence Report (which is
built from live watchlist data), this is a static onboarding artifact: it tells
the buyer exactly what their plan includes and how to use each capability.

Sections:
  1. Cover / Welcome
  2. What's included (the four capabilities)
  3. Vendor Scans — quota + how to scan
  4. Compliance Dashboard — traffic-light + alerts
  5. Vendor Directory — browse + filter the network
  6. Exports — CSV / PDF for tender spreadsheets
  7. Getting started checklist

Scaffolding (BaseDocTemplate/Frame/_draw_page/_section/_kv_table/_STYLES/
_xml_escape) mirrors cover_sheet_generator.py so both deliverables share one
visual system.
"""
from __future__ import annotations
from app.services.pdf_styles import get_unified_styles

import functools
import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Logo resolution mirrors cover_sheet_generator.py / pdf_service.py so all three
# generators find the same asset regardless of source-tree vs container layout.
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

# Bump when the visible structure of the welcome pack changes.
# v2: tier-aware header, plan-at-a-glance, and capability blocks (was Essentials-only).
# v3: multi-subsidiary + white-label capability blocks (Enterprise), and white-label
#     branding applied to the pack itself.
WELCOME_PACK_SCHEMA_VERSION = 3

# Monthly QUICK-scan quota for Buyer Essentials (buyer_starter). Mirrors
# BUYER_SCAN_LIMITS["buyer_starter"] QUICK in app/billing/enforcement.py — kept
# as a display fallback, not a runtime source of truth.
ESSENTIALS_SCAN_QUOTA = 10


def _fmt_count(n: int | None) -> str:
    """Render a quota count; None (from enforcement) means unlimited."""
    return "Unlimited" if n is None else str(n)


def _resolve_plan_spec(product_type: str | None, tier: str | None) -> Dict[str, Any]:
    """Resolve every tier-specific number and label the Welcome Pack renders
    from the structured sources of truth, so the pack always matches what the
    buyer is paying for (see acceptance bar in CLAUDE.md / pricing.py):

      • scan quotas   → app.billing.enforcement.BUYER_SCAN_LIMITS
      • seat cap      → app.billing.enforcement.PLAN_TO_MAX_SEATS
      • notarizations → app.core.models.ENTERPRISE_NOTARIZATION_LIMITS

    Deep-scan terminology differs by tier in the marketing language:
    Professional calls it "Deep Scan"; Enterprise calls it "Enhanced Scan".
    """
    from app.billing.enforcement import (
        scan_limit_for, max_seats_for, ENTERPRISE_PLAN_KEYS, BUYER_WHITE_LABEL_PLAN_KEYS,
    )
    from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS

    pk = (product_type or "buyer_starter").lower().strip()
    t = (tier or "starter").lower().strip()

    quick = scan_limit_for(pk, "QUICK")
    deep = scan_limit_for(pk, "DEEP")
    evidence = scan_limit_for(pk, "EVIDENCE")
    seats = max_seats_for(pk)
    notarizations = ENTERPRISE_NOTARIZATION_LIMITS.get(pk, 1)

    # Fallback so an unknown/legacy key never renders "0 Quick Scans".
    if not quick:
        quick = ESSENTIALS_SCAN_QUOTA

    # "Enhanced" is Enterprise's name for the deep-scan tier; "Deep" for Pro.
    deep_label = "Enhanced Scans" if t == "enterprise" else "Deep Scans"
    deep_label_singular = "Enhanced Scan" if t == "enterprise" else "Deep Scan"

    return {
        "tier": t,
        "quick": quick,
        "deep": deep,
        "evidence": evidence,
        "seats": seats,
        "notarizations": notarizations,
        "deep_label": deep_label,
        "deep_label_singular": deep_label_singular,
        "has_deep": bool(deep),
        "has_evidence": bool(evidence),
        # Resolved off the SAME key sets the API gates use, so the pack can never
        # document a capability the endpoint would 402 (which is exactly what the
        # activation email did before the gates were fixed).
        "has_subsidiaries": pk in ENTERPRISE_PLAN_KEYS,
        "has_white_label": pk in BUYER_WHITE_LABEL_PLAN_KEYS,
    }

PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
HEADER_H = 0.7 * inch
FOOTER_H = 0.45 * inch

NAVY    = colors.HexColor("#0f172a")
EMERALD = colors.HexColor("#10b981")
SLATE   = colors.HexColor("#64748b")
LIGHT   = colors.HexColor("#f8fafc")
BORDER  = colors.HexColor("#e2e8f0")
WHITE   = colors.white


def _hex_or(value: str | None, fallback):
    """Parse a white-label hex colour, falling back on anything unparseable."""
    if value:
        try:
            return colors.HexColor(value)
        except Exception:
            pass
    return fallback


def _draw_page(canvas, doc, *, header_label: str = "WELCOME PACK", branding: dict | None = None):
    branding = branding or {}
    # Buyer Enterprise white-label: the customer's primary colour becomes the
    # header band and their secondary the accent text, matching the mapping
    # pdf_layout.draw_page uses for the Procurement Intelligence Report.
    band_color = _hex_or(branding.get("secondary_color"), NAVY)
    accent_color = _hex_or(branding.get("primary_color"), EMERALD)

    canvas.saveState()
    canvas.setFillColor(band_color)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    # Explicit width — drawImage with only `height` silently skips on some
    # ReportLab builds. Logo is ~423×144 px → aspect ~2.94.
    logo_h = 0.48 * inch
    logo_w = logo_h * 2.94
    logo_y = PAGE_H - HEADER_H + (HEADER_H - logo_h) / 2
    logo_src, logo_aspect = _LOGO_PATH, 2.94
    if branding.get("logo_bytes"):
        try:
            reader = ImageReader(BytesIO(branding["logo_bytes"]))
            _lw, _lh = reader.getSize()
            logo_src, logo_aspect = reader, (_lw / _lh if _lh else 2.94)
        except Exception:
            logo_src, logo_aspect = _LOGO_PATH, 2.94
    logo_w = logo_h * logo_aspect
    logo_drawn = False
    if logo_src:
        try:
            canvas.drawImage(
                logo_src, MARGIN, logo_y,
                width=logo_w, height=logo_h,
                preserveAspectRatio=True, mask="auto",
            )
            logo_drawn = True
        except Exception:
            logo_drawn = False
    if not logo_drawn:
        canvas.setFillColor(accent_color)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(
            MARGIN, PAGE_H - HEADER_H + 0.26 * inch,
            (branding.get("report_header_text") or "BOOPPA")[:40].upper(),
        )

    canvas.setFillColor(accent_color)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(
        PAGE_W - MARGIN, PAGE_H - HEADER_H + 0.26 * inch,
        header_label,
    )

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, FOOTER_H, PAGE_W - MARGIN, FOOTER_H)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(
        MARGIN, FOOTER_H - 9,
        branding.get("footer_text") or "Booppa Smart Care LLC · booppa.io · Confidential",
    )
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 9, f"Page {doc.page}")
    canvas.restoreState()


_RAW_STYLES = get_unified_styles()
_STYLES: Dict[str, ParagraphStyle] = {
    "Normal":  ParagraphStyle("we_normal", fontSize=8.5, leading=13, textColor=colors.HexColor("#334155")),
    "h1":      ParagraphStyle("we_h1", fontSize=18, leading=22, textColor=NAVY, fontName="Helvetica-Bold"),
    "h2":      ParagraphStyle("we_h2", fontSize=10, leading=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=4, keepWithNext=1),
    "small":   ParagraphStyle("we_small", fontSize=7, leading=10, textColor=SLATE),
    "caption": ParagraphStyle("we_caption", fontSize=9, leading=13, textColor=colors.HexColor("#334155")),
    "body":    ParagraphStyle("we_body", fontSize=9, leading=14, textColor=colors.HexColor("#334155"), spaceAfter=4),
}


def _xml_escape(s: str) -> str:
    """Escape user-supplied text for ReportLab's Paragraph mini-XML."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _accent_hex(branding: dict | None) -> str:
    """Body accent colour — the white-label brand colour, else Booppa emerald.

    The header band alone is not what the activation email promises ("your own
    logo and brand colours"), so the section markers, capability chips and
    bullets follow it too.
    """
    raw = (branding or {}).get("primary_color")
    if raw:
        try:
            colors.HexColor(raw)
            return raw
        except Exception:
            pass
    return "#10b981"


def _section(title: str, *, page_break: bool = False, accent: str = "#10b981") -> list:
    """Section header; keepWithNext on h2 prevents the title widowing."""
    out: list = []
    out.append(PageBreak() if page_break else Spacer(1, 0.15 * inch))
    out.append(Paragraph(f'<font color="{accent}">■</font>  <b>{_xml_escape(title)}</b>', _STYLES["h2"]))
    out.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6))
    return out


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    def _val(v):
        return v if isinstance(v, Paragraph) else Paragraph(_xml_escape(str(v)), _STYLES["Normal"])
    data = [[Paragraph(f"<b>{_xml_escape(k)}</b>", _STYLES["Normal"]), _val(v)] for k, v in rows]
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


def _capability_block(number: int, title: str, blurb: str, bullets: list[str],
                      *, accent: str = "#10b981") -> KeepTogether:
    """A numbered capability card: emerald index + title, blurb, bullet list.
    KeepTogether so a card never splits across a page boundary.
    """
    head = Table(
        [[
            Paragraph(f'<font color="#ffffff"><b>{number}</b></font>',
                      ParagraphStyle("cap_idx", fontSize=11, leading=13, alignment=1, textColor=WHITE)),
            Paragraph(f"<b>{_xml_escape(title)}</b>",
                      ParagraphStyle("cap_title", fontSize=11, leading=14, textColor=NAVY, fontName="Helvetica-Bold")),
        ]],
        colWidths=[0.32 * inch, 6.38 * inch],
    )
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), _hex_or(accent, EMERALD)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow: list = [head, Spacer(1, 0.04 * inch),
                  Paragraph(_xml_escape(blurb), _STYLES["body"])]
    for b in bullets:
        flow.append(Paragraph(f'<font color="{accent}">•</font>  {_xml_escape(b)}', _STYLES["body"]))
    return KeepTogether(flow)


def generate_buyer_essentials_pack(data: Dict[str, Any]) -> bytes:
    """Build and return the Buyer Essentials welcome pack PDF bytes.

    Expected keys in `data`:
      company        — buyer organisation name (str)
      buyer_email    — buyer contact email (str)
      plan_label     — display label, default "Buyer Essentials"
      product_type   — raw plan key (e.g. "buyer_enterprise_monthly"); drives
                       every tier quota from the enforcement source of truth
      tier           — resolved tier: starter | pro | enterprise
      scan_quota     — legacy override for the QUICK quota (optional)
      white_label    — branding dict from white_label_and_sso
                       .resolve_branding_for_owner, or None (Buyer Enterprise only)
    """
    company    = data.get("company") or "Your organisation"
    buyer_email = data.get("buyer_email") or ""
    plan_label = data.get("plan_label") or "Buyer Essentials"
    now = datetime.now(timezone.utc).strftime("%d %b %Y")
    accent = _accent_hex(data.get("white_label"))

    spec = _resolve_plan_spec(data.get("product_type"), data.get("tier"))
    # Explicit legacy override still wins for the QUICK count if supplied.
    if data.get("scan_quota"):
        spec["quick"] = data["scan_quota"]
    scan_quota = _fmt_count(spec["quick"])

    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_H + 0.3 * inch,
        bottomMargin=FOOTER_H + 0.3 * inch,
    )
    header_label = f"{plan_label.upper()} · WELCOME PACK"
    # Buyer Enterprise white-label; None for every other tier → Booppa branding.
    branding = data.get("white_label") or None
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(
        id="main", frames=frame,
        onPage=functools.partial(_draw_page, header_label=header_label, branding=branding),
    )])

    story: list = []

    # ── Section 1: Cover / Welcome ────────────────────────────────────────────
    # The cover logo goes through platypus Image, which needs a source it can
    # read TWICE: once at construction for the size, again at draw time for the
    # pixels. A BytesIO is at EOF by the second read ("broken data stream when
    # reading image file") and an ImageReader isn't accepted at all, so spill the
    # white-label logo to a temp file — the same shape as _LOGO_PATH. Cleaned up
    # in the finally below, after doc.build has consumed it.
    _cover_logo = _LOGO_PATH
    _tmp_logo = None
    if branding and branding.get("logo_bytes"):
        try:
            import tempfile
            # Force a full decode NOW. ReportLab defers pixel decoding to draw
            # time, where a failure escapes doc.build and kills the whole pack —
            # a customer's bad logo upload must only cost them their branding.
            ImageReader(BytesIO(branding["logo_bytes"])).getRGBData()
            _fd, _tmp_logo = tempfile.mkstemp(suffix=".png")
            with os.fdopen(_fd, "wb") as _fh:
                _fh.write(branding["logo_bytes"])
            _cover_logo = _tmp_logo
        except Exception as e:
            logger.warning("[WelcomePack] white-label cover logo staging failed: %s", e)
            _cover_logo, _tmp_logo = _LOGO_PATH, None
    if _cover_logo:
        try:
            story.append(Spacer(1, 0.05 * inch))
            # lazy=0 decodes at construction. The default re-reads the source at
            # draw time, which is fine for a path but leaves an already-consumed
            # BytesIO at EOF → "broken data stream when reading image file".
            story.append(Image(_cover_logo, width=2.4 * inch, height=0.82 * inch,
                               kind="proportional"))
            story.append(Spacer(1, 0.12 * inch))
        except Exception as e:
            logger.warning("[WelcomePack] Body logo render failed: %s", e)

    story.append(Paragraph(f"Welcome to {_xml_escape(plan_label)}", _STYLES["h1"]))
    story.append(Paragraph(f"Onboarding pack for {_xml_escape(company)}", _STYLES["caption"]))
    story.append(Spacer(1, 0.12 * inch))
    story.append(Paragraph(
        "Your subscription is active. This pack walks through everything your plan "
        "includes and how to put each capability to work today.",
        _STYLES["body"],
    ))

    # Vendor-count phrasing: "up to N" reads wrong for an unlimited plan.
    quick_vendors_phrase = (
        "an unlimited number of vendors" if spec["quick"] is None
        else f"up to {scan_quota} vendors"
    )
    seats_display = _fmt_count(spec["seats"]) + (" seat" if spec["seats"] == 1 else " seats")

    story += _section("Your plan at a glance", accent=accent)
    glance_rows = [
        ("Plan", plan_label),
        ("Organisation", company),
        ("Account email", buyer_email or "—"),
        ("Quick Scans", f"{scan_quota} per month"),
    ]
    if spec["has_deep"]:
        glance_rows.append((spec["deep_label"], f"{_fmt_count(spec['deep'])} per month"))
    if spec["has_evidence"]:
        glance_rows.append(("Evidence Scans", f"{_fmt_count(spec['evidence'])} per month"))
    glance_rows += [
        ("Team seats", seats_display),
        ("Blockchain notarizations", f"{_fmt_count(spec['notarizations'])} per month"),
        ("Activated", now),
    ]
    story.append(_kv_table(glance_rows))

    # ── Section 2: What's included ────────────────────────────────────────────
    story += _section("What's included", accent=accent)
    cap_no = 1
    # Name only the lists we actually screen against — MAS prohibition-order
    # coverage depends on provider configuration (see csp_sanctions).
    from app.services.csp_sanctions import sanctions_coverage_label
    _sanctions_label = sanctions_coverage_label()

    quick_bullets = [
        f"{scan_quota} Quick Scans / month (re-viewing a vendor you already scanned this month is free)",
        "ACRA registration + entity status",
        f"Screening against {_sanctions_label}",
        "PDPA compliance flag",
    ]
    story.append(_capability_block(
        cap_no, "Vendor Scans",
        f"Run a Quick Scan on {quick_vendors_phrase} every month. Each scan checks "
        f"the vendor against the ACRA company registry and {_sanctions_label}, and raises a "
        "PDPA compliance flag.",
        quick_bullets,
        accent=accent,
    ))
    cap_no += 1

    # Pro+ deep-dive assessment (Deep / Enhanced scans). Wording mirrors the
    # pricing.py buyer_pro_* / buyer_enterprise_* descriptions.
    if spec["has_deep"]:
        story.append(Spacer(1, 0.08 * inch))
        deep_bullets = [
            f"{_fmt_count(spec['deep'])} {spec['deep_label']} / month",
            "11-dimension PDPA assessment with certifications check",
            "Financial-risk scoring",
            "Drift tracking across Deep-Scan parameters, month over month",
            "Side-by-side vendor comparison engine",
            "Customisable risk weightings",
        ]
        story.append(_capability_block(
            cap_no, f"{spec['deep_label']} & Comparison",
            f"Go beyond the registry check with the {spec['deep_label_singular']}: a "
            "full 11-dimension PDPA assessment, certifications and financial-risk "
            "scoring, drift tracking, and a comparison engine to rank shortlisted "
            "vendors with your own risk weightings.",
            deep_bullets,
            accent=accent,
        ))
        cap_no += 1

    # Enterprise evidence tier + governance controls.
    if spec["has_evidence"]:
        story.append(Spacer(1, 0.08 * inch))
        story.append(_capability_block(
            cap_no, "Evidence Scans & Enterprise Controls",
            f"Audit-ready due diligence for regulated procurement: {_fmt_count(spec['evidence'])} "
            "Evidence Scans a month, an on-chain evidence log, custom compliance "
            "frameworks, role-based access control, and a RESTful API for integration.",
            [
                f"{_fmt_count(spec['evidence'])} Evidence Scans / month",
                "On-chain (blockchain) evidence log",
                "Custom compliance frameworks",
                "Role-based access control (RBAC)",
                "RESTful API access",
            ],
            accent=accent,
        ))
        cap_no += 1

    # Multi-subsidiary + white-label. These are the two capabilities the Buyer
    # Enterprise activation email advertises with live CTAs; the pack previously
    # never mentioned either, so the "everything included" document contradicted
    # the email that shipped alongside it.
    if spec["has_subsidiaries"]:
        story.append(Spacer(1, 0.08 * inch))
        story.append(_capability_block(
            cap_no, "Multi-subsidiary management",
            "Run due diligence across multiple business units or legal entities from "
            "one account, with a group-level rollup rather than separate logins.",
            [
                "Add each subsidiary (name, UEN, country) under your organisation",
                "Group rollup across every linked entity",
                "Manage them at booppa.io/vendor/subsidiaries",
            ],
            accent=accent,
        ))
        cap_no += 1

    if spec["has_white_label"]:
        story.append(Spacer(1, 0.08 * inch))
        story.append(_capability_block(
            cap_no, "White-label reports",
            "This Welcome Pack and your monthly Procurement Intelligence Report carry "
            "your own logo and brand colours instead of Booppa's, so they can go "
            "straight to an internal stakeholder.",
            [
                "Upload your logo and set your brand colours",
                "Applied to your Procurement Intelligence Report and Welcome Pack",
                "Set it up at booppa.io/settings",
            ],
            accent=accent,
        ))
        cap_no += 1

    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        cap_no, "Compliance Dashboard",
        "A traffic-light view across every vendor you scan — CLEAN, WATCH, FLAGGED, or "
        "CRITICAL — with automatic email alerts when a vendor enters critical status.",
        [
            "At-a-glance RAG status for your whole portfolio",
            "Automatic alert when a vendor crosses your alert threshold",
            "Portfolio risk summary counts",
        ],
        accent=accent,
    ))
    cap_no += 1
    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        cap_no, "Vendor Directory",
        "Browse the Booppa vendor network with advanced filters so you can shortlist "
        "the right suppliers fast.",
        [
            "Filter by sector, size, and certifications",
            "Sort by compliance score and verification status",
            "See risk signal and trajectory before you engage",
        ],
        accent=accent,
    ))
    cap_no += 1
    story.append(Spacer(1, 0.08 * inch))
    story.append(_capability_block(
        cap_no, "Exports",
        "Export your scan results as CSV — ready to drop straight into a tender "
        "evaluation spreadsheet — or as a formatted PDF for the record.",
        [
            "CSV export of scan results for tender spreadsheets",
            "PDF export for filing and sharing",
        ],
        accent=accent,
    ))

    # ── Sections 3–6: How to use each capability ──────────────────────────────
    story += _section("Vendor Scans — how to scan", page_break=True, accent=accent)
    scan_rows = [("Quick Scan quota", f"{scan_quota} per month")]
    if spec["has_deep"]:
        scan_rows.append((f"{spec['deep_label']} quota", f"{_fmt_count(spec['deep'])} per month"))
    if spec["has_evidence"]:
        scan_rows.append(("Evidence Scan quota", f"{_fmt_count(spec['evidence'])} per month"))
    scan_rows += [
        ("What a Quick Scan covers", f"ACRA registry · {_sanctions_label} · PDPA flag"),
        ("Where", "Buyer dashboard → search a vendor → run a scan"),
        ("Quota reset", "1st of each calendar month"),
    ]
    story.append(_kv_table(scan_rows))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Re-viewing a vendor you have already scanned this month does not consume "
        "another credit — only the first scan of each vendor per month counts.",
        _STYLES["body"],
    ))

    story += _section("Compliance Dashboard — traffic-light & alerts", accent=accent)
    story.append(_kv_table([
        ("CLEAN", "No open risk signals — cleared for procurement."),
        ("WATCH", "Minor signals — monitor before you commit."),
        ("FLAGGED", "Open risk signals require resolution."),
        ("CRITICAL", "Severe signals — you receive an automatic alert email."),
    ]))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Set an alert threshold per vendor and Booppa emails you the moment a watched "
        "vendor crosses it, so you never miss a status change.",
        _STYLES["body"],
    ))

    story += _section("Vendor Directory — browse & filter", accent=accent)
    story.append(Paragraph(
        "Open the vendor directory to explore the network. Narrow the list with filters "
        "for sector, company size, and certifications, then sort by compliance score or "
        "verification status to build a shortlist.",
        _STYLES["body"],
    ))

    story += _section("Exports — CSV & PDF", accent=accent)
    story.append(Paragraph(
        "From your scan results, export a CSV to pull vendor status straight into a "
        "tender evaluation spreadsheet, or export a formatted PDF for your records and "
        "to share with approvers.",
        _STYLES["body"],
    ))

    # ── Section 7: Getting started ────────────────────────────────────────────
    story += _section("Getting started", accent=accent)
    for step in [
        "Open your buyer dashboard and add your first suppliers to the watchlist.",
        "Run a Quick Scan on the vendors you are evaluating this month.",
        "Set alert thresholds so you're notified if a vendor turns critical.",
        "Export your results to CSV when you're ready to build a tender shortlist.",
    ]:
        story.append(Paragraph(f'<font color="#10b981">✓</font>  {_xml_escape(step)}', _STYLES["body"]))

    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        "Questions? Reply to your welcome email or reach us at booppa.io. "
        "This pack is informational and describes plan features as of the activation date.",
        _STYLES["small"],
    ))

    try:
        doc.build(story)
    finally:
        if _tmp_logo:
            try:
                os.unlink(_tmp_logo)
            except OSError:
                pass
    return buf.getvalue()
