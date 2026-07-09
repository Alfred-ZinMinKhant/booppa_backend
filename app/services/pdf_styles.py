from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle, StyleSheet1

# Standard Booppa Brand Colors for PDFs
_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")

def get_unified_styles() -> StyleSheet1:
    """
    Returns the standard typography styles for Booppa PDF reports.
    """
    base = getSampleStyleSheet()
    
    for k in ["title", "sub", "h2", "body", "metric", "metric_lbl", "small", "cell", "cell_b", "big", "lbl", "mono"]:
        base.byAlias.pop(k, None)
        base.byName.pop(k, None)
        
    base.add(ParagraphStyle("title", parent=base["Title"], fontSize=20, textColor=_INK, spaceAfter=4))
    base.add(ParagraphStyle("sub", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#475569"), spaceAfter=2))
    base.add(ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, textColor=_INK, spaceBefore=16, spaceAfter=6))
    base.add(ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, textColor=colors.HexColor("#334155"), leading=14))
    base.add(ParagraphStyle("metric", parent=base["Normal"], fontSize=22, textColor=_INK, leading=24))
    base.add(ParagraphStyle("metric_lbl", parent=base["Normal"], fontSize=8, textColor=_MUTED, leading=11))
    base.add(ParagraphStyle("small", parent=base["Normal"], fontSize=7.5, textColor=_MUTED, leading=10))
    base.add(ParagraphStyle("cell", parent=base["Normal"], fontSize=8.5, textColor=colors.HexColor("#334155"), leading=11))
    base.add(ParagraphStyle("cell_b", parent=base["Normal"], fontSize=8.5, textColor=colors.HexColor("#334155"), leading=11, fontName="Helvetica-Bold"))
    base.add(ParagraphStyle("big", parent=base["Normal"], fontSize=28, textColor=_INK, leading=32, fontName="Helvetica-Bold"))
    base.add(ParagraphStyle("lbl", parent=base["Normal"], fontSize=7.5, textColor=_MUTED, leading=10))
    base.add(ParagraphStyle("mono", parent=base["Normal"], fontSize=7.5, textColor=colors.HexColor("#334155"), fontName="Courier", leading=10))
    
    return base
