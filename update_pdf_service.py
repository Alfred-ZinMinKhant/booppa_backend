import re

with open("app/services/pdf_service.py", "r") as f:
    content = f.read()

# 1. Rename _draw_page to draw_booppa_page
content = content.replace("def _draw_page(canvas, doc):", "def draw_booppa_page(canvas, doc):")
content = content.replace("onPage=_draw_page", "onPage=draw_booppa_page")
content = content.replace("for _draw_page", "for draw_booppa_page")

# 2. Extract get_booppa_styles
# It's currently inside PDFService as _build_styles
# Let's find it.
old_build_styles = """
    @staticmethod
    def _build_styles() -> dict:
        def ps(name, **kw) -> ParagraphStyle:
            return ParagraphStyle(name, **kw)

        return {"""

new_styles = """
def get_booppa_styles() -> dict:
    from reportlab.lib.styles import getSampleStyleSheet
    base = getSampleStyleSheet()
    
    def ps(name, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    return {
        # ── PDPA Monitor specific styles ──
        "pm_title": ps("pm_title", parent=base["Title"], fontSize=20, textColor=NAVY, spaceAfter=4),
        "pm_sub": ps("pm_sub", parent=base["Normal"], fontSize=10, textColor=SLATE, spaceAfter=2),
        "pm_h2": ps("pm_h2", parent=base["Heading2"], fontSize=13, textColor=NAVY, spaceBefore=14, spaceAfter=6),
        "pm_body": ps("pm_body", parent=base["Normal"], fontSize=9.5, textColor=TEXT_DARK, leading=14),
        "pm_big": ps("pm_big", parent=base["Normal"], fontSize=26, textColor=NAVY, leading=28),
        "pm_lbl": ps("pm_lbl", parent=base["Normal"], fontSize=8, textColor=SLATE, leading=11),
        "pm_cell": ps("pm_cell", parent=base["Normal"], fontSize=8.5, leading=11),
        "pm_small": ps("pm_small", parent=base["Normal"], fontSize=7.5, textColor=SLATE, leading=10),
"""
content = content.replace(old_build_styles, new_styles)

# Update __init__
content = content.replace("self._s = self._build_styles()", "self._s = get_booppa_styles()")

# 3. Add get_booppa_doc_template
template_code = """
def get_booppa_doc_template(buffer, title, report_type_label="AUDIT REPORT", is_pdpa=False, branding=None):
    from reportlab.platypus import SimpleDocTemplate
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=HEADER_H + 0.35 * inch,
        bottomMargin=FOOTER_H + 0.35 * inch,
        title=title,
    )
    doc._report_type_label = report_type_label
    doc._branding = branding
    if is_pdpa:
        _disc = (
            f"Automated compliance assessment by {COMPANY_NAME} · "
            f"{COMPANY_FRAMEWORK_VERSION} · Results reflect publicly accessible website elements at assessment date."
        )
        _disc2 = (
            "May be used as supporting evidence in procurement and regulatory contexts. "
            f"Does not substitute for legal counsel. {COMPANY_NAME}."
        )
        doc._pdpa_footer_lines = [_disc, _disc2]
    return doc

# ── PDFService ──"""
content = content.replace("# ── PDFService ─────────────────────────────────────────────────────────────────", template_code)

# 4. Refactor PDFService.generate_pdf
generate_pdf_old = """
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
                [PageTemplate(id="main", frames=[frame], onPage=draw_booppa_page)]
            )

            story = []

            # Compute framework type early — gates several sections below
            framework_raw = (report_data.get("framework") or "").upper()
            is_pdpa = framework_raw in {"PDPA", "PDPA_QUICK_SCAN"}
            is_notarization = "NOTARIZATION" in framework_raw
            is_rfp = "RFP KIT" in framework_raw or str(
                report_data.get("product_type") or ""
            ).startswith("rfp_")

            # Change 6(b/d): set PDPA footer disclaimer on doc object for draw_booppa_page
            if is_pdpa:
                _disc = (
                    f"Automated compliance assessment by {COMPANY_NAME} · "
                    f"{COMPANY_FRAMEWORK_VERSION} · Results reflect publicly accessible website elements at assessment date."
                )
                _disc2 = (
                    "May be used as supporting evidence in procurement and regulatory contexts. "
                    f"Does not substitute for legal counsel. {COMPANY_NAME}."
                )
                doc._pdpa_footer_lines = [_disc, _disc2]
"""

generate_pdf_new = """
            # Compute framework type early — gates several sections below
            framework_raw = (report_data.get("framework") or "").upper()
            is_pdpa = framework_raw in {"PDPA", "PDPA_QUICK_SCAN"}
            is_notarization = "NOTARIZATION" in framework_raw
            is_rfp = "RFP KIT" in framework_raw or str(
                report_data.get("product_type") or ""
            ).startswith("rfp_")

            report_type_label = (report_data.get("framework") or "AUDIT REPORT").upper().replace("_", " ")

            doc = get_booppa_doc_template(
                buffer=buffer,
                title="Booppa Report",
                report_type_label=report_type_label,
                is_pdpa=is_pdpa,
            )
            # Add PageTemplate since PDFService used BaseDocTemplate before, 
            # but wait, get_booppa_doc_template returns a SimpleDocTemplate!
            # SimpleDocTemplate already sets up a Frame and PageTemplate in its build() method!
            # So we don't need to add a PageTemplate manually if we call doc.build(story, onFirstPage=draw_booppa_page, onLaterPages=draw_booppa_page).
            # We'll just build it at the end.

            story = []
"""
content = content.replace(generate_pdf_old, generate_pdf_new)

# And at the end of generate_pdf, we must call build correctly.
# Let's find doc.build
build_old = "doc.build(story)"
build_new = "doc.build(story, onFirstPage=draw_booppa_page, onLaterPages=draw_booppa_page)"
content = content.replace(build_old, build_new)


with open("app/services/pdf_service.py", "w") as f:
    f.write(content)
