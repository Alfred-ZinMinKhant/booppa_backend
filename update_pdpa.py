import re

with open("app/services/pdpa_monitor_delta_generator.py", "r") as f:
    content = f.read()

# 1. Imports
content = content.replace("from app.services.pdf_logo import draw_logo_header\n", "")
content = content.replace(
    "from app.services.pdf_service import NAVY, EMERALD, SLATE, LIGHT_BG, BORDER, TEXT_DARK, WHITE",
    "from app.services.pdf_service import NAVY, EMERALD, SLATE, LIGHT_BG, BORDER, TEXT_DARK, WHITE, get_booppa_styles, get_booppa_doc_template, draw_booppa_page"
)

# 2. Remove _styles
styles_pattern = r"def _styles\(\):.*?(?=def generate_pdpa_monitor_report_pdf)"
content = re.sub(styles_pattern, "", content, flags=re.DOTALL)

# 3. Replace SimpleDocTemplate instantiation
old_doc_instantiation = """    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"PDPA Monitor Report — {company}",
    )

    s = _styles()"""

new_doc_instantiation = """    buf = BytesIO()
    doc = get_booppa_doc_template(
        buffer=buf,
        title=f"PDPA Monitor Report — {company}",
        report_type_label="PDPA MONITOR",
        is_pdpa=True,
    )

    s = get_booppa_styles()"""
content = content.replace(old_doc_instantiation, new_doc_instantiation)

# 4. Replace doc.build call
old_build = "    doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)"
new_build = "    doc.build(story, onFirstPage=draw_booppa_page, onLaterPages=draw_booppa_page)"
content = content.replace(old_build, new_build)

with open("app/services/pdpa_monitor_delta_generator.py", "w") as f:
    f.write(content)
