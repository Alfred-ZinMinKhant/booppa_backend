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

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch

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

# Logo geometry within the top margin.
_LOGO_H = 0.28 * inch
_LOGO_TOP_GAP = 0.18 * inch  # gap between page top edge and the logo


def draw_logo_header(canvas, doc) -> None:
    """ReportLab ``onPage`` callback: draw the Booppa logo at the top-left.

    Pass as ``onFirstPage`` / ``onLaterPages`` to ``SimpleDocTemplate.build``.
    The logo sits inside the page's top margin so it does not overlap content,
    with aspect ratio preserved. Silently no-ops if the asset is unavailable.
    """
    if not LOGO_PATH:
        return
    try:
        # Page width/height: prefer the doc's pagesize, fall back to A4.
        page_w, page_h = getattr(doc, "pagesize", None) or A4
        left = getattr(doc, "leftMargin", 0.75 * inch)
        y = page_h - _LOGO_TOP_GAP - _LOGO_H
        canvas.saveState()
        canvas.drawImage(
            LOGO_PATH,
            left,
            y,
            height=_LOGO_H,
            preserveAspectRatio=True,
            mask="auto",
        )
        canvas.restoreState()
    except Exception:
        # Never let a logo failure break document generation.
        pass
