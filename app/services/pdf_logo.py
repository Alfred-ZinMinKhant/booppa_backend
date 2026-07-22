"""Shared Booppa logo helpers for ReportLab PDF generators.

Single source of truth for the logo asset path plus a lightweight per-page
``onPage`` callback so every generated PDF carries the Booppa logo in its top
margin. The richer header/watermark logic in ``pdf_service.py`` and
``cover_sheet_generator.py`` is intentionally left untouched — those documents
already brand themselves; this module covers the many smaller generators that
previously emitted unbranded PDFs.

Drawing is always wrapped in ``try/except`` so a missing or unreadable asset can
never break document generation (these are paid fulfillment artifacts).
"""
from __future__ import annotations


import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader

# ── Logo resolution (tried at import time) — mirrors pdf_service.py ─────────────
_HERE = os.path.dirname(__file__)
_LOGO_CANDIDATES = [
    os.path.join(_HERE, "..", "..", "static", "logo.png"),
    "/app/static/logo.png",
    os.path.join(_HERE, "..", "..", "data", "logo.png"),
    "/app/data/logo.png",
]
LOGO_PATH: str | None = None
for _c in _LOGO_CANDIDATES:
    _abs = os.path.abspath(_c)
    if os.path.exists(_abs):
        LOGO_PATH = _abs
        break

# Header-band geometry (sits entirely within the page's top margin — every caller
# uses topMargin >= 0.6in, so a 0.5in band never overlaps body content).
_BAND_H = 0.5 * inch
_LOGO_H = 0.30 * inch
_INK = colors.HexColor("#0A0F1E")   # brand ink — matches BCEP / pdf_service header
_TEAL = colors.HexColor("#00C9A7")  # accent rule under the band

# Logo aspect ratio, resolved once. reportlab's canvas.drawImage SILENTLY drops a
# PNG when given only height + preserveAspectRatio=True, so we must pass an
# explicit width — computed from the asset's intrinsic aspect ratio.
_LOGO_ASPECT = 2.79  # fallback (1696x608); refined from the asset below
if LOGO_PATH:
    try:
        _iw, _ih = ImageReader(LOGO_PATH).getSize()
        if _ih:
            _LOGO_ASPECT = _iw / _ih
    except Exception:
        pass


def draw_logo_header(canvas, doc) -> None:
    """ReportLab ``onPage`` callback: draw a branded header band.

    Pass as ``onFirstPage`` / ``onLaterPages`` to ``SimpleDocTemplate.build``.
    The brand asset is a white wordmark on transparency, so it is drawn on a dark
    ink band (with a teal accent rule) — otherwise it is invisible on white pages.
    Falls back to a white wordmark text when the asset is unavailable. The band
    lives inside the top margin so it never overlaps content. Silently no-ops on
    any failure so a logo problem can never break document generation.

    White-label override: if the caller set ``doc._branding`` (same convention
    as ``pdf_service.py``'s header — a dict of ``logo_bytes``/``primary_color``/
    ``secondary_color``), the customer's own logo and band colours are drawn
    instead of Booppa's. Falls back to the Booppa asset on any decode failure.
    """
    try:
        # Page width/height: prefer the doc's pagesize, fall back to A4 (handles
        # landscape, e.g. procurement.py).
        page_w, page_h = getattr(doc, "pagesize", None) or A4
        left = getattr(doc, "leftMargin", 0.75 * inch)
        band_y = page_h - _BAND_H

        branding = getattr(doc, "_branding", None) or None
        band_color = _INK
        accent_color = _TEAL
        logo_reader = None
        logo_aspect = _LOGO_ASPECT
        if branding:
            if branding.get("secondary_color"):
                try:
                    band_color = colors.HexColor(branding["secondary_color"])
                except Exception:
                    pass
            if branding.get("primary_color"):
                try:
                    accent_color = colors.HexColor(branding["primary_color"])
                except Exception:
                    pass
            if branding.get("logo_bytes"):
                try:
                    from io import BytesIO
                    logo_reader = ImageReader(BytesIO(branding["logo_bytes"]))
                    _lw, _lh = logo_reader.getSize()
                    if _lh:
                        logo_aspect = _lw / _lh
                except Exception:
                    logo_reader = None

        canvas.saveState()
        # Band + accent rule along its bottom edge.
        canvas.setFillColor(band_color)
        canvas.rect(0, band_y, page_w, _BAND_H, fill=1, stroke=0)
        canvas.setStrokeColor(accent_color)
        canvas.setLineWidth(1.2)
        canvas.line(0, band_y, page_w, band_y)

        logo_y = band_y + (_BAND_H - _LOGO_H) / 2
        drawn = False
        # Explicit width required — canvas.drawImage silently drops a PNG when
        # given only height + preserveAspectRatio (see module docstring).
        if logo_reader is not None:
            try:
                canvas.drawImage(
                    logo_reader, left, logo_y,
                    width=_LOGO_H * logo_aspect, height=_LOGO_H, mask="auto",
                )
                drawn = True
            except Exception:
                drawn = False
        if not drawn and LOGO_PATH:
            try:
                canvas.drawImage(
                    LOGO_PATH, left, logo_y,
                    width=_LOGO_H * _LOGO_ASPECT, height=_LOGO_H, mask="auto",
                )
                drawn = True
            except Exception:
                drawn = False
        if not drawn:
            canvas.setFillColor(accent_color)
            canvas.setFont("Helvetica-Bold", 11)
            label = (branding or {}).get("report_header_text") or "BOOPPA INTELLIGENCE"
            canvas.drawString(left, band_y + (_BAND_H - 11) / 2, label.upper())
        canvas.restoreState()
    except Exception:
        # Never let a header failure break document generation.
        try:
            canvas.restoreState()
        except Exception:
            pass
