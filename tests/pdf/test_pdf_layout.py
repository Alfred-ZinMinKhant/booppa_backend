"""Unit tests for the shared PDF layout primitives.

These guard the three behaviours that the per-generator copies got wrong and
that the whole layout refresh depends on:

1. ``xml_escape`` must not crash on non-``str`` input (the copy in
   ``cover_sheet_generator`` did).
2. ``make_table`` must default to a repeating header row, so long tables never
   produce headerless continuation pages.
3. ``keep_together_safe`` must degrade rather than hand ReportLab an
   oversized ``KeepTogether``, which is silently clipped.
"""
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader
from reportlab.lib.units import inch
from reportlab.platypus import CondPageBreak, KeepTogether, PageBreak, Paragraph, Spacer

from app.services import pdf_layout as pl
from app.services.pdf_styles import get_unified_styles


# ── xml_escape ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ernst & Young", "Ernst &amp; Young"),
        ("Q&A", "Q&amp;A"),
        ("<script>", "&lt;script&gt;"),
        ("a > b", "a &gt; b"),
        ("plain", "plain"),
        ("", ""),
    ],
)
def test_xml_escape_escapes_paragraph_metacharacters(raw, expected):
    assert pl.xml_escape(raw) == expected


@pytest.mark.parametrize("raw,expected", [(None, ""), (0, "0"), (42, "42"), (Decimal("1.5"), "1.5"), (False, "False")])
def test_xml_escape_accepts_non_str(raw, expected):
    """The previous cover_sheet copy did `(s or "").replace(...)` with no
    `str()`, so an int/Decimal from the ORM raised AttributeError mid-build."""
    assert pl.xml_escape(raw) == expected


def test_xml_escape_output_is_paragraph_safe():
    """The real contract: escaped text must survive ReportLab's paraparser."""
    hostile = 'Ernst & Young <b>not a tag</b> & "quotes" 5 > 3'
    p = Paragraph(pl.xml_escape(hostile), get_unified_styles()["body"])
    w, h = p.wrap(pl.CONTENT_W, pl.PAGE_H)
    assert h > 0


def test_unescaped_markup_raises_in_paragraph():
    """Pins the hard failure mode escaping prevents: a well-formed but unclosed
    tag makes the paraparser raise and the whole PDF build fails. (ReportLab
    4.0.4 is lenient about bare `&` and unknown tags — see the test below for
    that softer, silent-corruption failure mode.)"""
    with pytest.raises(ValueError):
        Paragraph("<b>unclosed", get_unified_styles()["body"]).wrap(pl.CONTENT_W, pl.PAGE_H)


def test_escaping_preserves_literal_text_that_looks_like_markup():
    """The silent failure mode: ReportLab does not raise on `<notatag>`, it
    swallows it. Escaped, the customer's literal text survives to the page."""
    styles = get_unified_styles()
    raw = "Policy <notatag> applies"
    swallowed = Paragraph(raw, styles["body"])
    swallowed.wrap(pl.CONTENT_W, pl.PAGE_H)
    escaped = Paragraph(pl.xml_escape(raw), styles["body"])
    escaped.wrap(pl.CONTENT_W, pl.PAGE_H)
    # The escaped version is wider because the literal "<notatag>" is rendered
    # rather than being parsed away as an unknown tag.
    assert escaped.getPlainText() == raw
    assert swallowed.getPlainText() != raw


# ── make_table ─────────────────────────────────────────────────────────────────

def _rows(n: int) -> list[list[str]]:
    return [["Header A", "Header B"]] + [[f"r{i}a", f"r{i}b"] for i in range(n)]


def test_make_table_repeats_header_row_by_default():
    t = pl.make_table(_rows(3))
    assert t.repeatRows == 1


def test_make_table_splits_by_row():
    assert pl.make_table(_rows(3)).splitByRow == 1


def test_make_table_headerless_does_not_repeat():
    t = pl.make_table(_rows(3), header=False)
    assert t.repeatRows == 0


def test_make_table_repeat_header_can_be_disabled():
    t = pl.make_table(_rows(3), repeat_header=False)
    assert t.repeatRows == 0


def test_long_table_actually_splits_across_pages_with_header():
    """End-to-end: a table taller than a page must split, and every fragment
    after the first must carry the repeated header row."""
    t = pl.make_table(_rows(200), colWidths=[3 * inch, 3 * inch])
    avail_h = pl.PAGE_H - pl.TOP_MARGIN - pl.BOTTOM_MARGIN
    t.wrap(pl.CONTENT_W, avail_h)
    parts = t.split(pl.CONTENT_W, avail_h)
    assert len(parts) > 1, "a 200-row table should not fit on one page"
    continuation = parts[1]
    assert continuation._cellvalues[0] == ["Header A", "Header B"]


def test_make_table_accepts_style_extra():
    t = pl.make_table(_rows(2), style_extra=[("SPAN", (0, 0), (1, 0))])
    assert t is not None


# ── kv_table ───────────────────────────────────────────────────────────────────

def test_kv_table_escapes_values():
    t = pl.kv_table([("Company", "Ernst & Young"), ("Note", None)])
    # Renders without the paraparser raising.
    t.wrap(pl.CONTENT_W, pl.PAGE_H)
    assert t.repeatRows == 0  # label/value table has no header row to repeat


def test_kv_table_passes_flowables_through():
    badge = pl.severity_badge("HIGH")
    t = pl.kv_table([("Severity", badge)])
    assert t._cellvalues[0][1] is badge


# ── section ────────────────────────────────────────────────────────────────────

def test_section_uses_conditional_break_by_default():
    """The default must be conditional — unconditional PageBreak before short
    sections is what produced the near-blank pages."""
    flows = pl.section("Findings")
    assert any(isinstance(f, CondPageBreak) for f in flows)
    assert not any(isinstance(f, PageBreak) and not isinstance(f, CondPageBreak) for f in flows)


def test_section_hard_break_still_available():
    flows = pl.section("Appendix", page_break=True)
    assert any(type(f) is PageBreak for f in flows)


def test_section_title_has_orphan_protection():
    para = next(f for f in pl.section("Findings") if isinstance(f, Paragraph))
    assert para.style.keepWithNext == 1


def test_section_escapes_title():
    flows = pl.section("Q&A Coverage")
    para = next(f for f in flows if isinstance(f, Paragraph))
    assert "&amp;" in para.text


# ── keep_together_safe ─────────────────────────────────────────────────────────

def test_keep_together_safe_wraps_short_block():
    styles = get_unified_styles()
    block = [Paragraph("Short heading", styles["h2"]), Paragraph("One line.", styles["body"])]
    out = pl.keep_together_safe(block)
    assert len(out) == 1 and isinstance(out[0], KeepTogether)


def test_keep_together_safe_degrades_for_oversized_block():
    """A block taller than a page must NOT come back as a KeepTogether —
    ReportLab silently drops the overflow, losing paid-for content."""
    styles = get_unified_styles()
    block = [
        Paragraph("Heading", styles["h2"]),
        Paragraph("word " * 8000, styles["body"]),
    ]
    out = pl.keep_together_safe(block)
    assert not any(isinstance(f, KeepTogether) for f in out)
    assert out[-len(block):] == block, "all content must survive, unwrapped"


def test_keep_together_safe_degrades_via_cond_break_not_keep_with_next():
    """Orphan protection for an oversized block must come from CondPageBreak.
    Using keepWithNext here strands a near-blank page: ReportLab tests whether
    the successor fits in the remaining space, and a >1-page successor never
    does, so it force-breaks every time."""
    styles = get_unified_styles()
    heading = Paragraph("Heading", styles["body"])  # style without keepWithNext
    block = [heading, Paragraph("word " * 8000, styles["body"])]
    out = pl.keep_together_safe(block)
    assert isinstance(out[0], CondPageBreak)
    assert out[1:] == block
    assert heading.getKeepWithNext() == 0


def test_keep_together_safe_clears_style_level_keep_with_next():
    """The shared h2 style sets keepWithNext=1; the degraded path must override
    it on the instance (without mutating the shared stylesheet)."""
    styles = get_unified_styles()
    heading = Paragraph("Heading", styles["h2"])
    pl.keep_together_safe([heading, Paragraph("word " * 8000, styles["body"])])
    assert heading.getKeepWithNext() == 0
    assert get_unified_styles()["h2"].keepWithNext == 1, "shared stylesheet mutated"


def test_oversized_block_does_not_strand_a_near_blank_page():
    """End-to-end regression: short section followed by an oversized block must
    not leave a page containing only the short section."""
    buf = BytesIO()
    styles = get_unified_styles()
    doc = pl.build_doc(buf, title="T")
    story = pl.section("Short Section") + [Paragraph("Three lines only.", styles["body"])]
    story += pl.keep_together_safe(
        [Paragraph("Oversized Block", styles["h2"]), Paragraph("word " * 4000, styles["body"])]
    )
    pl.render(doc, story)

    reader = PdfReader(BytesIO(buf.getvalue()))
    # The short section's page must also carry the start of the oversized block.
    first = reader.pages[0].extract_text() or ""
    assert "Short Section" in first
    assert "Oversized Block" in first, "oversized block was pushed to its own page"


def test_keep_together_safe_handles_empty_and_none():
    assert pl.keep_together_safe([]) == []
    assert pl.keep_together_safe([None, None]) == []


def test_oversized_block_is_taller_than_the_frame():
    """Pins the premise keep_together_safe rests on: the block used in the
    degradation tests really is taller than one page, so wrapping it in a
    KeepTogether would be the clipping bug rather than a no-op."""
    styles = get_unified_styles()
    _w, h = Paragraph("word " * 8000, styles["body"]).wrap(pl.CONTENT_W, pl.PAGE_H)
    assert h > pl.PAGE_H - pl.TOP_MARGIN - pl.BOTTOM_MARGIN


# ── build_doc / render ─────────────────────────────────────────────────────────

def test_build_doc_uses_canonical_geometry():
    doc = pl.build_doc(BytesIO(), title="T")
    assert doc.leftMargin == doc.rightMargin == pl.MARGIN
    assert doc.topMargin == pl.TOP_MARGIN
    assert doc.bottomMargin == pl.BOTTOM_MARGIN
    assert (doc.pagesize[0], doc.pagesize[1]) == (pl.PAGE_W, pl.PAGE_H)


def test_build_doc_landscape():
    doc = pl.build_doc(BytesIO(), title="T", landscape=True)
    assert doc.pagesize[0] > doc.pagesize[1]


def test_render_produces_a_pdf_with_page_numbers():
    buf = BytesIO()
    styles = get_unified_styles()
    doc = pl.build_doc(buf, title="Test", header_label="TEST REPORT")
    story = pl.section("Section One") + [
        pl.make_table(_rows(120), colWidths=[3 * inch, 3 * inch]),
    ]
    pl.render(doc, story)
    out = buf.getvalue()
    assert out.startswith(b"%PDF")
    assert len(out) > 1000


def test_render_survives_hostile_branding():
    """Bad white-label colours must not break a paid deliverable."""
    buf = BytesIO()
    doc = pl.build_doc(
        buf, title="T",
        branding={"primary_color": "not-a-color", "secondary_color": "#zzzzzz",
                  "logo_bytes": b"not-a-png", "footer_text": "Acme Ltd"},
    )
    pl.render(doc, [Paragraph("Body", get_unified_styles()["body"])])
    assert buf.getvalue().startswith(b"%PDF")


# ── stylesheet orphan protection ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ["title", "h2", "h3"])
def test_shared_heading_styles_have_keep_with_next(name):
    assert get_unified_styles()[name].keepWithNext == 1


def test_existing_style_names_are_preserved():
    """23 modules import this stylesheet by name — none may disappear."""
    styles = get_unified_styles()
    for name in ["title", "sub", "h2", "body", "metric", "metric_lbl",
                 "small", "cell", "cell_b", "big", "lbl", "mono"]:
        assert styles[name] is not None


# ── footer / page-number collision ─────────────────────────────────────────────

def test_footer_disclaimer_shrinks_instead_of_colliding_with_page_number():
    """The disclaimer used to run straight into "Page N of M" ("…at assessment
    datePage 3"). It must shrink to clear the reserved zone — and shrink rather
    than truncate, because it is a legal statement."""
    from reportlab.pdfgen.canvas import Canvas

    c = Canvas(BytesIO())
    long_line = (
        "Automated compliance assessment by Booppa Smart Care LLC · BACF-v1.0 · "
        "Results reflect publicly accessible website elements at assessment date."
    )
    avail = pl.CONTENT_W - pl.PAGENO_RESERVE_W
    size = pl._shrink_to_fit(c, [long_line], "Helvetica-Oblique", 7, avail)
    assert size < 7, "a line this long must be shrunk"
    assert c.stringWidth(long_line, "Helvetica-Oblique", size) <= avail


def test_shrink_to_fit_leaves_short_lines_alone():
    from reportlab.pdfgen.canvas import Canvas

    c = Canvas(BytesIO())
    assert pl._shrink_to_fit(c, ["short"], "Helvetica", 7, 400) == 7


def test_rendered_footer_does_not_overlap_the_page_number():
    """End-to-end: extract the footer line and assert the disclaimer text and
    the page number are not run together."""
    buf = BytesIO()
    doc = pl.build_doc(
        buf, title="T",
        footer_lines=["Automated compliance assessment by Booppa Smart Care LLC · "
                      "BACF-v1.0 · Results reflect publicly accessible website "
                      "elements at assessment date."],
    )
    pl.render(doc, [Paragraph("Body", get_unified_styles()["body"])])
    text = PdfReader(BytesIO(buf.getvalue())).pages[0].extract_text() or ""
    assert "Page 1 of 1" in text
    assert "datePage" not in text, "disclaimer ran into the page number"


# ── logo weight ────────────────────────────────────────────────────────────────

def test_logo_is_downscaled_so_pdfs_are_not_a_megabyte_of_png():
    """The raw asset is a 736 KB 1696px PNG embedded at full resolution, which
    made a 5 KB policy document render at 968 KB."""
    buf = BytesIO()
    doc = pl.build_doc(buf, title="T", header_label="X")
    pl.render(doc, [Paragraph("Body", get_unified_styles()["body"])])
    assert len(buf.getvalue()) < 300_000


def test_logo_flowable_returns_none_rather_than_raising():
    img = pl.logo_flowable(1.2 * inch, 0.4 * inch)
    assert img is None or hasattr(img, "wrap")
