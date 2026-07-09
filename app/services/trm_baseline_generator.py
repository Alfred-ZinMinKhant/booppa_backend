"""MAS TRM Baseline Assessment — tangible suite deliverable.

Standard Suite / Pro Suite activation initialises all 13 MAS TRM control
domains, but until now the buyer received only an email saying so — no
artifact. A forensic audit flagged this: "13 domains initialised" with no
baseline document to show for a SGD 1,800–4,500/mo subscription.

This module renders a one-shot baseline assessment PDF from the seeded
TrmControl rows: every domain, its control reference, its current status, and
the recommended next action. It is intentionally a STARTING-POINT document
(everything is "Not Started" on day one) — its value is giving the buyer a
structured, board-presentable inventory of what the engagement will work
through, not a finished gap analysis.
"""
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from app.services.pdf_styles import get_unified_styles
from app.core.company import COMPANY_NAME
from app.services.pdf_logo import draw_logo_header

logger = logging.getLogger(__name__)

# Bump when the visible structure of the baseline PDF changes.
# v2: added an Initial Gap Analysis section (per-domain requirement, priority,
#     and baseline gap) + an optional Configuration & Provisioning Status section
#     (Pro Suite evidence of what's provisioned). Closes the audit gaps "no
#     initial gap analysis report" and "zero evidence of active configuration".
# v3: assessed entity now rendered as an explicit, unambiguous "Assessed Entity"
#     line (the CUSTOMER); Booppa demoted to a clear "Prepared by" attribution so
#     the header can never be read as "Booppa" being the assessed organisation
#     (audit: baselines were headed "Booppa", not the customer — "unacceptable"
#     for MAS use).
TRM_BASELINE_SCHEMA_VERSION = 3

# Per-domain initial gap framework — what each MAS TRM domain requires + its
# supervisory priority. Used to render a real starting gap analysis on day one
# (everything is "Not Started", so the gap is establishing the control). Once
# the customer runs the AI gap analysis in the workspace, the per-control
# `gap_analysis` text overrides the template line below.
_DOMAIN_GAP = {
    "Technology Risk Governance": ("Board/senior-management oversight, a risk-appetite statement, and a TRM framework with defined roles.", "High"),
    "IT Project and Change Management": ("A documented SDLC and change-management process with approvals, testing gates, and rollback plans.", "Medium"),
    "Technology Operations": ("Capacity, availability, and configuration management with documented operating procedures and monitoring.", "Medium"),
    "IT Outsourcing and Vendor Management": ("Due diligence, contractual safeguards, and ongoing monitoring of material service providers (incl. cloud).", "High"),
    "Cyber Security": ("Layered controls — perimeter, endpoint, vulnerability & patch management, and continuous threat monitoring.", "High"),
    "Data and Information Management": ("Data classification, encryption in transit/at rest, and access-on-need with audit logging.", "High"),
    "Customer Awareness and Education": ("Customer security advisories and anti-phishing / scam education materials.", "Low"),
    "Incident Management": ("An incident response plan with severity tiers, escalation, and MAS notification timelines.", "High"),
    "IT Audit": ("Independent periodic IT audit coverage with tracked findings and remediation.", "Medium"),
    "Business Continuity and Disaster Recovery": ("BCP/DR plans with defined RTO/RPO and at least annual tested recovery.", "High"),
    "Technology Testing": ("Security testing — VAPT and source-code review — on a risk-based schedule.", "Medium"),
    "Cloud Computing": ("Cloud governance: shared-responsibility mapping, configuration baselines, and key management.", "Medium"),
    "Authentication and Access Management": ("MFA, least-privilege RBAC, privileged-access management, and periodic access recertification.", "High"),
}
_PRIORITY_COLOR = {"High": "#dc2626", "Medium": "#b45309", "Low": "#475569"}

_STATUS_LABEL = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "compliant": "Compliant",
    "gap": "Gap Identified",
}
_STATUS_COLOR = {
    "not_started": "#92400e",
    "in_progress": "#1d4ed8",
    "compliant": "#065f46",
    "gap": "#dc2626",
}


def _xml_escape(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    return s


def generate_trm_baseline_pdf(data: Dict[str, Any]) -> bytes:
    """Render the baseline PDF.

    Expected `data`:
      company_name: str
      plan_label:   str  (e.g. "Pro Suite")
      generated_at: ISO str (optional)
      controls:     list of {domain, control_ref, status, risk_rating, gap_analysis}
    """
    s = get_unified_styles("trm_")
    company = data.get("company_name") or "Your Organisation"
    plan_label = data.get("plan_label") or "Suite"
    controls: List[Dict[str, Any]] = data.get("controls") or []
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title=f"MAS TRM Baseline — {company}",
    )
    story: list = []

    story.append(Paragraph("MAS TRM Baseline Assessment", s["title"]))
    # The assessed entity is ALWAYS the customer — label it explicitly so the
    # header cannot be mistaken for Booppa (audit fix).
    story.append(Paragraph(
        f"<b>Assessed Entity:</b> {_xml_escape(company)}", s["sub"]))
    story.append(Paragraph(
        f"{_xml_escape(plan_label)} &middot; Generated {gen_at}", s["small"]))
    story.append(Paragraph(
        f"Prepared by {_xml_escape(COMPANY_NAME)} on behalf of {_xml_escape(company)}",
        s["small"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Scope", s["h2"]))
    story.append(Paragraph(
        "This baseline inventories all 13 control domains of the Monetary Authority of "
        "Singapore (MAS) Technology Risk Management (TRM) Guidelines as initialised for your "
        "organisation. Every domain begins at <b>Not Started</b>; work each one in your TRM "
        "workspace, run the AI gap analysis, and attach evidence to move a domain to "
        "<b>Compliant</b>. This document is a structured starting point, not a statement of "
        "compliance.",
        s["body"]))
    story.append(Spacer(1, 6))

    # Status summary
    counts: Dict[str, int] = {}
    for c in controls:
        st = (c.get("status") or "not_started")
        counts[st] = counts.get(st, 0) + 1
    summary = " &middot; ".join(
        f"{_STATUS_LABEL.get(k, k)}: {v}" for k, v in sorted(counts.items())
    ) or "No controls initialised"
    story.append(Paragraph(f"<b>Summary:</b> {summary} (of {len(controls)} domains)", s["body"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Control Domains", s["h2"]))
    header = [
        Paragraph("<b>Ref</b>", s["cell_b"]),
        Paragraph("<b>MAS TRM Domain</b>", s["cell_b"]),
        Paragraph("<b>Status</b>", s["cell_b"]),
        Paragraph("<b>Next Action</b>", s["cell_b"]),
    ]
    rows = [header]
    status_row_styles = []
    for i, c in enumerate(controls, start=1):
        st = (c.get("status") or "not_started")
        color_hex = _STATUS_COLOR.get(st, "#334155")
        status_para = Paragraph(
            f'<font color="{color_hex}">{_STATUS_LABEL.get(st, st)}</font>', s["cell"],
        )
        next_action = (
            "Run AI gap analysis & attach evidence" if st == "not_started"
            else "Complete in-progress evidence" if st == "in_progress"
            else "Remediate identified gap" if st == "gap"
            else "Maintain & re-attest"
        )
        rows.append([
            Paragraph(_xml_escape(c.get("control_ref") or f"TRM-{i}"), s["cell"]),
            Paragraph(_xml_escape(c.get("domain") or "—"), s["cell"]),
            status_para,
            Paragraph(next_action, s["cell"]),
        ])

    table = Table(rows, colWidths=[0.7 * inch, 2.9 * inch, 1.1 * inch, 2.2 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    # ── Initial Gap Analysis ────────────────────────────────────────────────
    story.append(Paragraph("Initial Gap Analysis", s["h2"]))
    story.append(Paragraph(
        "A starting gap assessment for each domain. On day one every domain is "
        "<b>Not Started</b>, so the gap is establishing and evidencing the control "
        "below. As you run the AI gap analysis and attach evidence in your TRM "
        "workspace, regenerate this report to replace these with your assessed gaps.",
        s["body"]))
    story.append(Spacer(1, 6))
    gap_header = [
        Paragraph("<b>MAS TRM Domain</b>", s["cell_b"]),
        Paragraph("<b>Priority</b>", s["cell_b"]),
        Paragraph("<b>Gap &amp; First Control to Establish</b>", s["cell_b"]),
    ]
    gap_rows = [gap_header]
    for c in controls:
        domain = c.get("domain") or "—"
        requirement, priority = _DOMAIN_GAP.get(domain, ("Establish and document this control with evidence.", "Medium"))
        # Customer-assessed gap (from the workspace) overrides the template line.
        assessed = (c.get("gap_analysis") or "").strip()
        gap_text = assessed or f"No control evidence on file yet. Establish: {requirement}"
        pcolor = _PRIORITY_COLOR.get(priority, "#475569")
        gap_rows.append([
            Paragraph(_xml_escape(domain), s["cell"]),
            Paragraph(f'<font color="{pcolor}"><b>{priority}</b></font>', s["cell"]),
            Paragraph(_xml_escape(gap_text), s["cell"]),
        ])
    gap_table = Table(gap_rows, colWidths=[2.4 * inch, 0.8 * inch, 3.7 * inch], repeatRows=1)
    gap_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(gap_table)
    story.append(Spacer(1, 14))

    # ── Configuration & Provisioning Status (Pro Suite evidence) ────────────
    provisioning = data.get("provisioning") or []
    if provisioning:
        story.append(Paragraph("Configuration &amp; Provisioning Status", s["h2"]))
        story.append(Paragraph(
            "Tangible evidence of what your subscription has provisioned. "
            "&ldquo;Active&rdquo; capabilities are live now; &ldquo;Ready&rdquo; capabilities are "
            "provisioned and waiting on a one-time setup step at the linked page.",
            s["body"]))
        story.append(Spacer(1, 6))
        prov_header = [
            Paragraph("<b>Capability</b>", s["cell_b"]),
            Paragraph("<b>Status</b>", s["cell_b"]),
            Paragraph("<b>Detail / Next Step</b>", s["cell_b"]),
        ]
        prov_rows = [prov_header]
        for p in provisioning:
            st = (p.get("status") or "Ready")
            pcolor = "#065f46" if st.lower() == "active" else "#1d4ed8"
            prov_rows.append([
                Paragraph(_xml_escape(p.get("capability") or "—"), s["cell"]),
                Paragraph(f'<font color="{pcolor}"><b>{_xml_escape(st)}</b></font>', s["cell"]),
                Paragraph(_xml_escape(p.get("detail") or "—"), s["cell"]),
            ])
        prov_table = Table(prov_rows, colWidths=[2.2 * inch, 0.9 * inch, 3.8 * inch], repeatRows=1)
        prov_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(prov_table)
        story.append(Spacer(1, 14))

    story.append(Paragraph("Recommended First Steps", s["h2"]))
    for step in (
        "Prioritise Cyber Security (TRM-5), Authentication &amp; Access Management (TRM-13), and "
        "Incident Management (TRM-8) — these carry the highest supervisory attention.",
        "Use the AI gap analysis in your TRM workspace to draft a gap narrative and risk rating per domain.",
        "Attach existing policies and evidence to each control to move it toward Compliant.",
        "Re-generate this baseline any time to track how many domains have advanced.",
    ):
        story.append(Paragraph(f"&bull; {step}", s["body"]))
        story.append(Spacer(1, 3))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"This document is generated by {COMPANY_NAME} for informational "
        "purposes only and does not constitute legal or regulatory advice or a statement of MAS "
        "compliance.", s["small"]))

    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
    return buf.getvalue()
