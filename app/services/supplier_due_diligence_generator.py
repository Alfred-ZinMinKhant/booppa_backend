"""Supplier Due-Diligence Certificate / Snapshot — the buyer's tangible artifact.

One page capturing a single watched supplier's *verified state on a date*: current
verification status, Trust/Compliance score + month-over-month drift, PDPA posture,
and — when anchored — a Polygon transaction hash the buyer can independently verify.

Two products share this one renderer so they can never drift apart:

  * **Instant watchlist-add snapshot** (#3) — fired the moment a buyer watches a
    supplier. Un-anchored for Starter (HTML card / plain PDF); the buyer gets
    proof of the supplier's state from action #1, no waiting for the monthly cycle.
  * **Due-Diligence Certificate** (#2) — the anchored version. The SHA-256 of the
    rendered PDF is written on-chain (idempotent per hash) so the buyer can drop
    the file into their own audit / procurement decision record.

`anchor` is decided by the *caller* (the Celery task), not here, because anchoring
is async and gas-bearing. In demo/test-checkout mode the caller passes a mock tx
hash instead of hitting the chain — see `demo_tx_hash`. This module only renders
and gathers read-only data; it never writes to the DB or the chain.
"""
import hashlib
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict

from app.services.tx_utils import is_real_onchain_tx

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.pdf_styles import get_unified_styles
from app.services.pdf_logo import draw_logo_header
from app.core.company import COMPANY_NAME

logger = logging.getLogger(__name__)

SUPPLIER_DUE_DILIGENCE_SCHEMA_VERSION = 1

_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")
_PAPER = colors.HexColor("#f8fafc")


def _xml_escape(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def demo_tx_hash(evidence_hash: str) -> str:
    """Deterministic, realistic-looking mock tx hash for demo/test-checkout mode.

    Never hits the chain (no gas). Derived from the evidence hash so the same
    certificate renders the same hash, but clearly a demo value.
    """
    digest = hashlib.sha256(f"demo:{evidence_hash}".encode()).hexdigest()
    return "0x" + digest[:64]




def _delta_str(d) -> str:
    if d is None or not isinstance(d, int):
        return '<font color="#64748b">—</font>'
    if d > 0:
        return f'<font color="#16a34a">▲ {d}</font>'
    if d < 0:
        return f'<font color="#dc2626">▼ {abs(d)}</font>'
    return "0"


def generate_certificate_pdf(data: Dict[str, Any]) -> bytes:
    """Render the one-page certificate. `data` keys (all optional except supplier_name):
      supplier_name, buyer_company, resolved (bool), risk_signal, procurement_readiness,
      trust_score, compliance_score, trust_delta, compliance_delta, generated_at,
      tx_hash (str | None), anchored (bool), is_certificate (bool — cert vs snapshot),
      notes.
    """
    s = get_unified_styles()
    supplier = data.get("supplier_name") or "Supplier"
    buyer = data.get("buyer_company") or "Your Organisation"
    resolved = bool(data.get("resolved"))
    gen_at = data.get("generated_at") or datetime.now(timezone.utc).strftime("%d %B %Y")
    tx_hash = data.get("tx_hash")
    anchored = bool(data.get("anchored"))
    is_cert = bool(data.get("is_certificate"))
    # Demo/test-checkout artifact for a fictional supplier — mark it unmistakably so
    # a screenshotted "certificate" can never be mistaken for a real one.
    sample_data = bool(data.get("sample_data"))

    def _num(v):
        return "—" if v is None else str(v)

    doc_title = "Supplier Due-Diligence Certificate" if is_cert else "Supplier Verification Snapshot"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"{'[SAMPLE] ' if sample_data else ''}{doc_title} — {supplier}",
    )
    story: list = []

    story.append(Paragraph(doc_title, s["title"]))
    story.append(Paragraph(_xml_escape(supplier), s["sub"]))
    story.append(Paragraph(
        f"Prepared for {_xml_escape(buyer)} &middot; As of {gen_at} &middot; {COMPANY_NAME}",
        s["small"]))
    story.append(Spacer(1, 16))

    # First-page sample-data callout (a persistent per-page strip is drawn by the
    # page callback below). Rendered as a prominent amber notice box.
    if sample_data:
        _sample_style = ParagraphStyle(
            "sample_callout", parent=s["body"],
            textColor=colors.HexColor("#7c2d12"), fontName="Helvetica-Bold", fontSize=10,
        )
        _sc = Table(
            [[Paragraph(
                "SAMPLE DATA — illustrative only. This is a demo of the artifact you receive "
                "when you add a supplier to your watchlist. The supplier above is a fictional "
                "example, not a real company, and nothing here is a statement about anyone's "
                "compliance.", _sample_style)]],
            colWidths=[6.4 * inch],
        )
        _sc.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef3c7")),
            ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#d97706")),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(_sc)
        story.append(Spacer(1, 14))

    # ── Verified state cards ────────────────────────────────────────────────────
    if resolved:
        status = data.get("risk_signal") or data.get("procurement_readiness") or "MONITORED"
    else:
        status = "UNRATED"
    cards = [[
        [Paragraph(_num(data.get("trust_score")), s["metric"]), Paragraph("TRUST SCORE", s["metric_lbl"]),
         Paragraph(_delta_str(data.get("trust_delta")) + " vs last scan", s["small"])],
        [Paragraph(_num(data.get("compliance_score")), s["metric"]), Paragraph("PDPA / COMPLIANCE", s["metric_lbl"]),
         Paragraph(_delta_str(data.get("compliance_delta")) + " vs last scan", s["small"])],
        [Paragraph(_xml_escape(str(status)), s["metric_lbl"]), Paragraph("VERIFICATION STATUS", s["metric_lbl"]), Paragraph("", s["small"])],
    ]]
    ct = Table(cards, colWidths=[2.2 * inch, 2.2 * inch, 2.0 * inch])
    ct.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), _PAPER),
        ("BOX", (0, 0), (-1, -1), 0.5, _RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(ct)
    story.append(Spacer(1, 12))

    if not resolved:
        story.append(Paragraph(
            "This supplier is not yet a claimed profile on the platform, so live scores are "
            "not available. Verified scores populate once they complete verification.", s["body"]))

    notes = data.get("notes")
    if notes:
        story.append(Paragraph("Your notes", s["h2"]))
        story.append(Paragraph(_xml_escape(notes), s["body"]))

    # ── Verification / anchor block ─────────────────────────────────────────────
    story.append(Paragraph("Verification record", s["h2"]))
    # Only surface a transaction hash when the record is genuinely anchored on a
    # real, confirmed on-chain tx. A demo/test-checkout value (demo_tx_hash) is
    # shape-valid but never mined (anchored=False), so it must NOT be rendered as
    # a "Transaction reference" — that was how a fabricated hash reached buyers.
    if is_cert and anchored and is_real_onchain_tx(tx_hash):
        chain = "Polygon Amoy"
        story.append(Paragraph(
            f"This record's SHA-256 fingerprint is anchored on {chain}. Transaction:",
            s["body"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(_xml_escape(tx_hash), s["mono"]))
    else:
        story.append(Paragraph(
            "This snapshot reflects the supplier's verified state at the timestamp above. "
            "An anchored, independently-verifiable certificate is available on Pro and "
            "Enterprise plans.", s["body"]))

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        f"Generated by {COMPANY_NAME} for procurement due-diligence purposes only. Supplier "
        "scores and risk signals are data-driven estimates, not guarantees, and not a statement "
        "of any supplier's regulatory compliance.", s["small"]))

    if sample_data:
        def _on_page(canvas, doc_):
            draw_logo_header(canvas, doc_)
            # Persistent SAMPLE-DATA strip near the foot of every page so the
            # warning survives even if a single page is printed or forwarded.
            try:
                page_w, _ = getattr(doc_, "pagesize", None) or A4
                strip_h = 0.24 * inch
                y = 0.42 * inch
                canvas.saveState()
                canvas.setFillColor(colors.HexColor("#d97706"))
                canvas.rect(0, y, page_w, strip_h, fill=1, stroke=0)
                canvas.setFillColor(colors.white)
                canvas.setFont("Helvetica-Bold", 8)
                canvas.drawCentredString(
                    page_w / 2.0, y + strip_h / 2 - 3,
                    "SAMPLE DATA — illustrative demo, not a real supplier certificate",
                )
                canvas.restoreState()
            except Exception:
                pass
        on_first = on_later = _on_page
    else:
        on_first = on_later = draw_logo_header

    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    return buf.getvalue()


def build_certificate_data(
    db,
    buyer_user_id: str,
    vendor_ref: str,
    *,
    vendor_name: str | None = None,
    notes: str | None = None,
    is_certificate: bool = False,
    sample_data: bool = False,
) -> Dict[str, Any]:
    """Gather a supplier's current verified state for the certificate/snapshot.

    Read-only. Resolves the watchlist `vendor_ref` to a claimed vendor User and
    pulls its VendorScore / VendorStatusSnapshot / trend via the shared buyer
    insights helpers, degrading to an UNRATED record when unresolvable.

    `sample_data=True` marks a demo/test-checkout artifact built for a fictional
    supplier; it drives the unmissable SAMPLE-DATA banner on the rendered PDF.
    """
    from app.core.models import User
    from app.services.buyer_procurement_insights import (
        _resolve_watchlist_vendor_user, _supplier_status,
    )

    buyer = db.query(User).filter(User.id == buyer_user_id).first()
    buyer_company = (getattr(buyer, "company", None) or "Your Organisation")

    data: Dict[str, Any] = {
        "supplier_name": vendor_name or vendor_ref,
        "buyer_company": buyer_company,
        "resolved": False,
        "notes": notes,
        "is_certificate": is_certificate,
        "sample_data": sample_data,
    }
    try:
        vuid = _resolve_watchlist_vendor_user(db, vendor_ref)
        if vuid:
            data["resolved"] = True
            data.update(_supplier_status(db, vuid))
    except Exception as e:  # pragma: no cover
        logger.warning("[DueDiligence] status lookup failed for ref=%s: %s", vendor_ref, e)
    # Only banner as SAMPLE when a demo fire drew a fictional supplier that did not
    # resolve to real DB state. A demo fire against the buyer's own real watched
    # supplier carries genuine data and must not be mislabelled.
    data["sample_data"] = bool(sample_data) and not data["resolved"]
    return data


def evidence_hash_for(pdf_bytes: bytes) -> str:
    """SHA-256 hex of the rendered certificate — the value anchored on-chain."""
    return hashlib.sha256(pdf_bytes).hexdigest()
