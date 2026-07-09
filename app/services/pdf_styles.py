from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Standard Booppa Brand Colors for PDFs
_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_RULE = colors.HexColor("#e2e8f0")

def get_unified_styles(prefix: str = "") -> dict:
    """
    Returns the standard typography styles for Booppa PDF reports.
    The prefix argument (e.g. 'vp_') ensures the ParagraphStyle names are unique 
    in the global reportlab registry to prevent shadowing bugs when multiple 
    reports are generated in the same process.
    """
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(f"{prefix}title", parent=base["Title"], fontSize=20, textColor=_INK, spaceAfter=4),
        "sub": ParagraphStyle(f"{prefix}sub", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#475569"), spaceAfter=2),
        "h2": ParagraphStyle(f"{prefix}h2", parent=base["Heading2"], fontSize=13, textColor=_INK, spaceBefore=16, spaceAfter=6),
        "body": ParagraphStyle(f"{prefix}body", parent=base["Normal"], fontSize=9.5, textColor=colors.HexColor("#334155"), leading=14),
        "metric": ParagraphStyle(f"{prefix}metric", parent=base["Normal"], fontSize=22, textColor=_INK, leading=24),
        "metric_lbl": ParagraphStyle(f"{prefix}metric_lbl", parent=base["Normal"], fontSize=8, textColor=_MUTED, leading=11),
        "small": ParagraphStyle(f"{prefix}small", parent=base["Normal"], fontSize=7.5, textColor=_MUTED, leading=10),
        "cell": ParagraphStyle(f"{prefix}cell", parent=base["Normal"], fontSize=8.5, textColor=colors.HexColor("#334155"), leading=11),
        "mono": ParagraphStyle(f"{prefix}mono", parent=base["Normal"], fontSize=7.5, textColor=colors.HexColor("#334155"), fontName="Courier", leading=10),
    }
