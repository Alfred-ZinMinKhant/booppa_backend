import sys

content = """from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle, StyleSheet1

# Standard Booppa Brand Colors for PDFs
_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")

def get_unified_styles(prefix: str = "") -> StyleSheet1:
    \"\"\"
    Returns the standard typography styles for Booppa PDF reports.
    The prefix argument ensures uniqueness.
    \"\"\"
    base = getSampleStyleSheet()
    
    base.add(ParagraphStyle(f"{prefix}title", parent=base["Title"], fontSize=20, textColor=_INK, spaceAfter=4))
    base.add(ParagraphStyle(f"{prefix}sub", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#475569"), spaceAfter=2))
    base.add(ParagraphStyle(f"{prefix}h2", parent=base["Heading2"], fontSize=13, textColor=_INK, spaceBefore=16, spaceAfter=6))
    base.add(ParagraphStyle(f"{prefix}body", parent=base["Normal"], fontSize=9.5, textColor=colors.HexColor("#334155"), leading=14))
    base.add(ParagraphStyle(f"{prefix}metric", parent=base["Normal"], fontSize=22, textColor=_INK, leading=24))
    base.add(ParagraphStyle(f"{prefix}metric_lbl", parent=base["Normal"], fontSize=8, textColor=_MUTED, leading=11))
    base.add(ParagraphStyle(f"{prefix}small", parent=base["Normal"], fontSize=7.5, textColor=_MUTED, leading=10))
    base.add(ParagraphStyle(f"{prefix}cell", parent=base["Normal"], fontSize=8.5, textColor=colors.HexColor("#334155"), leading=11))
    base.add(ParagraphStyle(f"{prefix}cell_b", parent=base["Normal"], fontSize=8.5, textColor=colors.HexColor("#334155"), leading=11, fontName="Helvetica-Bold"))
    base.add(ParagraphStyle(f"{prefix}big", parent=base["Normal"], fontSize=28, textColor=_INK, leading=32, fontName="Helvetica-Bold"))
    base.add(ParagraphStyle(f"{prefix}lbl", parent=base["Normal"], fontSize=7.5, textColor=_MUTED, leading=10))
    base.add(ParagraphStyle(f"{prefix}mono", parent=base["Normal"], fontSize=7.5, textColor=colors.HexColor("#334155"), fontName="Courier", leading=10))
    
    return base
"""

with open("app/services/pdf_styles.py", "w") as f:
    f.write(content)
