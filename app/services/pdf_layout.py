"""Shared layout primitives for every Booppa ReportLab generator.

Single source of truth for page geometry, brand colours, page furniture
(header band + footer + page numbers), XML escaping, and the table/section
flowables. Before this module existed each of the ~25 generators under
``app/services/`` re-implemented all of it: ``_xml_escape`` was defined 17
times with three divergent semantics, ``_kv_table`` 5 times, the header/footer
canvas callback 6 times, and six different page geometries were in use.

That duplication is why pagination was bad — there was no one place to set
``repeatRows`` on tables, ``keepWithNext`` on headings, or a "keep N inches
together" guard. Those defaults now live here:

* :func:`make_table` sets ``repeatRows=1`` by default, so a long table that
  spills onto the next page repeats its header row instead of continuing
  headerless.
* :func:`section` emits a ``CondPageBreak`` rather than an unconditional
  ``PageBreak``, so a section only starts a new page when there isn't enough
  room left for it to begin meaningfully — no more near-blank pages.
* :func:`keep_together_safe` wraps content in ``KeepTogether`` only when it
  actually fits on a page. A plain ``KeepTogether`` around unbounded content
  (a free-text AI answer, a ROPA record with many purposes) is silently
  *clipped* by ReportLab when it overflows — content the customer paid for
  just disappears. This degrades to orphan protection instead.

Every drawing routine is wrapped in ``try/except``: these are paid fulfillment
artifacts and a branding glitch must never fail document generation.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable, Sequence

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape as _landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.pdf_logo import LOGO_PATH
from app.services.pdf_styles import get_unified_styles

__all__ = [
    "xml_escape",
    "PAGE_W", "PAGE_H", "MARGIN", "CONTENT_W", "HEADER_H", "FOOTER_H",
    "NAVY", "EMERALD", "SLATE", "LIGHT", "BORDER", "WHITE", "INK", "TEAL",
    "SEVERITY_COLORS", "severity_badge",
    "page_geometry", "draw_page", "build_doc", "NumberedCanvas",
    "make_table", "kv_table", "section", "keep_together_safe",
    "COMPANY_LEGAL_FOOTER",
]


# ── Escaping ───────────────────────────────────────────────────────────────────

def xml_escape(s: Any) -> str:
    """Escape text for ReportLab's Paragraph mini-XML.

    Paragraph treats ``&`` as an entity start and ``<`` as a tag start, so an
    unescaped company name like "Ernst & Young" or a query-string URL makes the
    paraparser raise and the whole PDF fail to build.

    Accepts any type — several of the 17 previous copies took ``str`` only and
    crashed on the ``None``/``int``/``Decimal`` values that routinely arrive
    from the ORM and from AI-generated dicts.
    """
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── Geometry ───────────────────────────────────────────────────────────────────
# One canonical A4 geometry. Previously in use across the tree: 0.6in, 0.7in,
# 0.75in, 0.8in, 1in (reportlab default) and raw points (52pt). 0.75in is the
# value the two flagship documents (pdf_service, cover_sheet) already used.

PAGE_W, PAGE_H = A4
MARGIN = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
HEADER_H = 0.65 * inch
FOOTER_H = 0.45 * inch

# Gap between the page furniture and the first/last line of body content.
_BODY_GAP = 0.35 * inch

TOP_MARGIN = HEADER_H + _BODY_GAP
BOTTOM_MARGIN = FOOTER_H + _BODY_GAP


def page_geometry(landscape: bool = False) -> tuple[float, float]:
    """Page size as ``(width, height)``, honouring landscape."""
    return _landscape(A4) if landscape else A4


# ── Brand palette ──────────────────────────────────────────────────────────────
# Promoted from the private _INK/_MUTED/_RULE in pdf_styles.py plus the
# per-generator NAVY/EMERALD/SLATE re-declarations.

NAVY    = colors.HexColor("#0f172a")
INK     = colors.HexColor("#0A0F1E")   # header band — slightly deeper than NAVY
EMERALD = colors.HexColor("#10b981")
TEAL    = colors.HexColor("#00C9A7")   # accent rule under the header band
SLATE   = colors.HexColor("#64748b")
LIGHT   = colors.HexColor("#f8fafc")
BORDER  = colors.HexColor("#e2e8f0")
WHITE   = colors.white
BODY_INK = colors.HexColor("#334155")

SEVERITY_COLORS: dict[str, colors.Color] = {
    "CRITICAL": colors.HexColor("#7f1d1d"),
    "HIGH":     colors.HexColor("#dc2626"),
    "MEDIUM":   colors.HexColor("#f59e0b"),
    "LOW":      colors.HexColor("#10b981"),
    "INFO":     SLATE,
}

COMPANY_LEGAL_FOOTER = "Booppa Smart Care LLC · booppa.io · Confidential"

# Width reserved at the right of the footer for "Page N of M".
PAGENO_RESERVE_W = 62.0


def severity_badge(severity: str) -> Paragraph:
    """A white-on-colour severity chip, sized for a table cell."""
    sev = (severity or "MEDIUM").upper()
    style = ParagraphStyle(
        f"sev_{sev.lower()}",
        fontSize=7, leading=9, textColor=WHITE, fontName="Helvetica-Bold",
        alignment=1, backColor=SEVERITY_COLORS.get(sev, SLATE), borderPadding=2,
    )
    return Paragraph(xml_escape(sev), style)


# ── Logo ───────────────────────────────────────────────────────────────────────
# Resolved once by pdf_logo. The aspect ratio must be read from the asset and
# passed to drawImage as an explicit `width`: canvas.drawImage SILENTLY drops a
# PNG when given only `height` + preserveAspectRatio=True.

_LOGO_ASPECT = 2.79  # fallback for the 1696x608 wordmark
_LOGO_SRC: Any = LOGO_PATH

# The asset is a 736 KB 1696×608 PNG. ReportLab embeds it at full resolution,
# so every generated PDF carried ~0.7 MB of logo (a 5 KB CSP policy document
# became 968 KB) even though the header renders it 24 pt tall. Downscale once at
# import: 640 px still exceeds what any use here needs at print resolution.
_LOGO_MAX_W = 640

if LOGO_PATH:
    try:
        _iw, _ih = ImageReader(LOGO_PATH).getSize()
        if _ih:
            _LOGO_ASPECT = _iw / _ih
        if _iw > _LOGO_MAX_W:
            from PIL import Image as _PILImage

            _img = _PILImage.open(LOGO_PATH)
            _img = _img.convert("RGBA") if _img.mode not in ("RGB", "RGBA") else _img
            _img = _img.resize(
                (_LOGO_MAX_W, max(1, round(_LOGO_MAX_W / _LOGO_ASPECT))),
                _PILImage.LANCZOS,
            )
            _buf = BytesIO()
            _img.save(_buf, format="PNG", optimize=True)
            _buf.seek(0)
            _LOGO_SRC = ImageReader(_buf)
    except Exception:
        _LOGO_SRC = LOGO_PATH


def logo_flowable(width: float, height: float | None = None, **kwargs):
    """A cover-page logo ``Image`` using the downscaled asset.

    Generators that did ``Image(LOGO_PATH, ...)`` embedded the full 736 KB PNG a
    second time, on top of the header copy. Returns ``None`` when no logo asset
    resolved, so callers can simply skip it.
    """
    from reportlab.platypus import Image as _Image

    if _LOGO_SRC is None:
        return None
    try:
        return _Image(
            _LOGO_SRC, width=width,
            height=height if height is not None else width / _LOGO_ASPECT,
            **kwargs,
        )
    except Exception:
        return None


def _branding_of(doc) -> dict:
    return getattr(doc, "_branding", None) or {}


def _resolve_logo(branding: dict) -> tuple[Any, float]:
    """Return ``(image_reader_or_path, aspect)`` honouring a white-label logo."""
    raw = branding.get("logo_bytes")
    if raw:
        try:
            reader = ImageReader(BytesIO(raw))
            lw, lh = reader.getSize()
            return reader, (lw / lh if lh else _LOGO_ASPECT)
        except Exception:
            pass
    return _LOGO_SRC, _LOGO_ASPECT


# ── Page furniture ─────────────────────────────────────────────────────────────

def draw_page(canvas, doc) -> None:
    """``onPage`` callback drawing the header band, logo, label and footer.

    Merges the three previous implementations (``pdf_logo.draw_logo_header``,
    ``pdf_service.draw_booppa_page``, ``cover_sheet_generator._draw_page``).

    Reads optional attributes off ``doc``, all set by :func:`build_doc`:

    ``_branding``            white-label dict: ``logo_bytes``, ``primary_color``,
                             ``secondary_color``, ``report_header_text``,
                             ``footer_text``
    ``_header_label``        right-aligned caption in the header band
    ``_footer_lines``        extra italic disclaimer lines above the footer rule
    ``_watermark``           draw the diagonal low-opacity logo watermark

    Deliberately does NOT draw the page number — :class:`NumberedCanvas` does
    that on a second pass, once the total page count is known.
    """
    try:
        page_w, page_h = getattr(doc, "pagesize", None) or A4
        left = getattr(doc, "leftMargin", MARGIN)
        right = page_w - getattr(doc, "rightMargin", MARGIN)
        branding = _branding_of(doc)

        band_color = _hex_or(branding.get("secondary_color"), INK)
        accent_color = _hex_or(branding.get("primary_color"), TEAL)

        canvas.saveState()

        if getattr(doc, "_watermark", False):
            _draw_watermark(canvas, page_w, page_h, branding)

        # ── Header band + accent rule ────────────────────────────────────────
        band_y = page_h - HEADER_H
        canvas.setFillColor(band_color)
        canvas.rect(0, band_y, page_w, HEADER_H, fill=1, stroke=0)
        canvas.setStrokeColor(accent_color)
        canvas.setLineWidth(1.2)
        canvas.line(0, band_y, page_w, band_y)

        logo_h = 0.34 * inch
        logo_y = band_y + (HEADER_H - logo_h) / 2
        logo_src, aspect = _resolve_logo(branding)
        drawn = False
        if logo_src is not None:
            try:
                canvas.drawImage(
                    logo_src, left, logo_y,
                    width=logo_h * aspect, height=logo_h, mask="auto",
                )
                drawn = True
            except Exception:
                drawn = False
        if not drawn:
            label = branding.get("report_header_text") or "BOOPPA"
            canvas.setFillColor(WHITE)
            canvas.setFont("Helvetica-Bold", 13)
            canvas.drawString(left, logo_y + 0.08 * inch, xml_escape(label).upper())

        header_label = getattr(doc, "_header_label", None)
        if header_label:
            canvas.setFillColor(accent_color)
            canvas.setFont("Helvetica-Bold", 7.5)
            canvas.drawRightString(right, band_y + 0.24 * inch, header_label)

        # ── Footer ───────────────────────────────────────────────────────────
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(left, FOOTER_H, right, FOOTER_H)

        canvas.setFillColor(SLATE)
        # Reserve room on the right for "Page N of M", which NumberedCanvas
        # stamps on the first footer line. Without this the legal disclaimer
        # runs straight into it ("…at assessment datePage 3").
        text_w = (right - left) - PAGENO_RESERVE_W
        footer_lines = getattr(doc, "_footer_lines", None)
        if footer_lines:
            # Shrink to fit rather than truncate — these are legal disclaimers
            # and dropping their tail would change what the document says.
            size = _shrink_to_fit(canvas, footer_lines, "Helvetica-Oblique", 7, text_w)
            canvas.setFont("Helvetica-Oblique", size)
            y = FOOTER_H - 9
            for line in footer_lines:
                canvas.drawString(left, y, line)
                y -= size + 2
        else:
            canvas.setFont("Helvetica", 6.5)
            canvas.drawString(
                left, FOOTER_H - 9,
                _fit(canvas,
                     branding.get("footer_text") or COMPANY_LEGAL_FOOTER,
                     "Helvetica", 6.5, text_w),
            )

        canvas.restoreState()
    except Exception:
        # Page furniture must never break a paid deliverable.
        try:
            canvas.restoreState()
        except Exception:
            pass


def _shrink_to_fit(canvas, lines, font: str, size: float, max_w: float,
                   floor: float = 5.0) -> float:
    """Largest size ≤ ``size`` at which every line fits ``max_w``.

    Used for the footer disclaimer, where truncation is not acceptable: the
    text is a legal statement and losing its tail changes what the deliverable
    claims.
    """
    if max_w <= 0:
        return size
    while size > floor and any(
        canvas.stringWidth(str(ln or ""), font, size) > max_w for ln in lines
    ):
        size -= 0.25
    return size


def _fit(canvas, text: str, font: str, size: float, max_w: float) -> str:
    """Truncate ``text`` with an ellipsis so it fits ``max_w`` at ``font``/``size``."""
    text = str(text or "")
    if max_w <= 0 or canvas.stringWidth(text, font, size) <= max_w:
        return text
    ell = "…"
    while text and canvas.stringWidth(text + ell, font, size) > max_w:
        text = text[:-1]
    return text.rstrip() + ell


def _hex_or(value: str | None, fallback: colors.Color) -> colors.Color:
    if not value:
        return fallback
    try:
        return colors.HexColor(value)
    except Exception:
        return fallback


def _draw_watermark(canvas, page_w: float, page_h: float, branding: dict) -> None:
    src, aspect = _resolve_logo(branding)
    if src is None:
        return
    try:
        canvas.saveState()
        canvas.setFillAlpha(0.06)
        wm_w = 5.5 * inch
        canvas.translate(page_w / 2, page_h / 2)
        canvas.rotate(35)
        canvas.drawImage(
            src, -wm_w / 2, -(wm_w / aspect) / 2,
            width=wm_w, height=wm_w / aspect, mask="auto",
        )
        canvas.restoreState()
    except Exception:
        try:
            canvas.restoreState()
        except Exception:
            pass


class NumberedCanvas(_canvas.Canvas):
    """Canvas that stamps ``Page N of M`` once the total is known.

    ReportLab's ``onPage`` callbacks run while the document is still being laid
    out, so they cannot know the page count — which is why every previous
    implementation could only print a bare ``Page N``. This buffers each page's
    state and writes the footer on ``save()``, when ``M`` is finally known.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states: list[dict] = []

    def showPage(self):  # noqa: N802 — ReportLab API
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total: int) -> None:
        try:
            page_w = self._pagesize[0]
            self.saveState()
            self.setFillColor(SLATE)
            self.setFont("Helvetica", 6.5)
            self.drawRightString(
                page_w - MARGIN, FOOTER_H - 9,
                f"Page {self._pageNumber} of {total}",
            )
            self.restoreState()
        except Exception:
            try:
                self.restoreState()
            except Exception:
                pass


def build_doc(
    buffer,
    *,
    title: str,
    header_label: str = "",
    footer_lines: Sequence[str] | None = None,
    branding: dict | None = None,
    landscape: bool = False,
    watermark: bool = False,
) -> SimpleDocTemplate:
    """Return a ``SimpleDocTemplate`` with the canonical geometry and furniture.

    Build it with ``doc.build(story, onFirstPage=draw_page,
    onLaterPages=draw_page, canvasmaker=NumberedCanvas)`` — or just call
    :func:`render` which does that for you.
    """
    pagesize = page_geometry(landscape)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=title,
    )
    doc._header_label = header_label
    doc._footer_lines = list(footer_lines) if footer_lines else None
    doc._branding = branding or None
    doc._watermark = watermark
    return doc


def render(doc: SimpleDocTemplate, story: list) -> None:
    """Build ``story`` into ``doc`` with the shared furniture and page numbers."""
    doc.build(
        story,
        onFirstPage=draw_page,
        onLaterPages=draw_page,
        canvasmaker=NumberedCanvas,
    )


# ── Flowables ──────────────────────────────────────────────────────────────────

_STYLES = get_unified_styles()


def make_table(
    data: list[list[Any]],
    colWidths: Sequence[float] | None = None,
    *,
    header: bool = True,
    zebra: bool = True,
    repeat_header: bool = True,
    grid: bool = True,
    style_extra: Iterable[tuple] | None = None,
    **kwargs,
) -> Table:
    """Build a table that paginates correctly.

    ``repeat_header`` (default on) sets ``repeatRows=1`` so a table continuing
    onto the next page redraws its header row. Only 13 of ~45 tables in the
    codebase did this before; the rest produced headerless continuation pages
    that readers cannot interpret.

    ``splitByRow=1`` is always set so a long table splits at a row boundary
    rather than being pushed whole onto the next page (or clipped).
    """
    repeat_rows = 1 if (header and repeat_header) else 0
    table = Table(
        data,
        colWidths=colWidths,
        repeatRows=repeat_rows,
        splitByRow=1,
        **kwargs,
    )

    style: list[tuple] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]
    if grid:
        style.append(("GRID", (0, 0), (-1, -1), 0.3, BORDER))
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
        if zebra:
            style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]))
    elif zebra:
        style.append(("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT, WHITE]))

    if style_extra:
        style.extend(style_extra)
    table.setStyle(TableStyle(style))
    return table


def kv_table(rows: Sequence[tuple[str, Any]], *, label_w: float = 2.2 * inch) -> Table:
    """Two-column label/value table. Values are escaped unless already flowables."""
    body_style = ParagraphStyle(
        "kv_value", fontSize=8, leading=12, textColor=BODY_INK
    )
    label_style = ParagraphStyle(
        "kv_label", parent=body_style, fontName="Helvetica-Bold"
    )

    def _val(v):
        return v if isinstance(v, Flowable) else Paragraph(xml_escape(v), body_style)

    data = [[Paragraph(xml_escape(k), label_style), _val(v)] for k, v in rows]
    return make_table(
        data,
        colWidths=[label_w, CONTENT_W - label_w],
        header=False,
        zebra=True,
    )


def section(
    title: str,
    styles=None,
    *,
    min_space: float = 1.2 * inch,
    page_break: bool = False,
) -> list:
    """Section heading: rule + title, with sane break behaviour.

    By default emits a ``CondPageBreak(min_space)`` — the section moves to a new
    page only when fewer than ``min_space`` remains, so it never starts with the
    heading stranded at the foot of a page and never forces a near-blank page.

    Pass ``page_break=True`` only for a genuine hard break (the start of a new
    major part of the document). Several call sites used an unconditional
    ``PageBreak`` for what was really this conditional case, which is where the
    blank pages came from.

    The ``h2`` style carries ``keepWithNext=1`` (see ``pdf_styles.py``) so the
    title is guaranteed to travel with at least the first line of its content.
    """
    styles = styles or _STYLES
    out: list = []
    if page_break:
        out.append(PageBreak())
    else:
        out.append(CondPageBreak(min_space))
        out.append(Spacer(1, 0.15 * inch))
    out.append(Paragraph(f'<font color="#10b981">■</font>  <b>{xml_escape(title)}</b>', styles["h2"]))
    out.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6))
    return out


def _usable_page_height(doc=None) -> float:
    if doc is not None:
        try:
            return float(doc.height)
        except Exception:
            pass
    return PAGE_H - TOP_MARGIN - BOTTOM_MARGIN


def keep_together_safe(
    flowables: Sequence[Flowable],
    *,
    avail_width: float = CONTENT_W,
    max_height: float | None = None,
    min_lead: float = 1.5 * inch,
) -> list:
    """Keep ``flowables`` on one page — but only when they actually fit.

    ``KeepTogether`` around content taller than a page makes ReportLab *silently
    drop* it. That was happening to whole ROPA records, multi-page AI answers and
    RFP declaration bodies: the customer paid for content that never rendered.

    This measures the block first. If it fits, it returns a single
    ``KeepTogether``.

    If it doesn't fit, the block is returned unwrapped, preceded by a
    ``CondPageBreak(min_lead)`` so its first element still gets a meaningful
    amount of page beneath it, and ``keepWithNext`` is *cleared* on that first
    element.

    Clearing it is essential, not incidental. ReportLab honours
    ``keepWithNext`` by testing whether the next flowable fits in the remaining
    space; a successor taller than a full page can never fit, so it force-breaks
    every time and strands a near-blank page — the exact defect this module
    exists to remove. Since the shared ``h2``/``h3`` styles set
    ``keepWithNext=1``, an oversized block led by a heading hits this without
    the override. It is applied to the flowable *instance*, so the shared
    stylesheet is untouched.
    """
    items = [f for f in flowables if f is not None]
    if not items:
        return []
    limit = max_height if max_height is not None else _usable_page_height()

    total = 0.0
    for f in items:
        try:
            _w, h = f.wrap(avail_width, limit)
            total += h
        except Exception:
            # Unmeasurable (rare); assume it may be large and don't risk clipping.
            total = limit + 1
            break
        if total > limit:
            break

    if total <= limit:
        return [KeepTogether(list(items))]

    try:
        items[0].keepWithNext = 0
    except Exception:
        pass
    return [CondPageBreak(min(min_lead, limit))] + list(items)
