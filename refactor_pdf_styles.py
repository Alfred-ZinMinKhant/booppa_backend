import re
import os

files = [
    ("app/services/buyer_procurement_report_generator.py", "bp_"),
    ("app/services/supplier_due_diligence_generator.py", "dd_"),
    ("app/services/trm_baseline_generator.py", "trm_"),
    ("app/services/vendor_artifacts_generator.py", "va_"),
    ("app/services/vendor_pdpa_snapshot_generator.py", "vps_"),
    ("app/services/vendor_pro_report_generator.py", "vp_"),
    ("app/services/vendor_snapshot_generator.py", "vs_"),
]

for filepath, prefix in files:
    with open(filepath, 'r') as f:
        content = f.read()
    
    # 1. Remove the def _styles(): block completely
    # It starts with def _styles(): and ends with a closing brace `}` and a newline.
    # We use a regex that handles the block gracefully.
    styles_pattern = re.compile(r'def _styles\(\):.*?\n    \}\n', re.DOTALL)
    content = styles_pattern.sub('', content)

    # 2. Add the import if not present
    if "get_unified_styles" not in content:
        import_stmt = "from app.services.pdf_styles import get_unified_styles\n"
        # Find the last standard reportlab import or just put it after the first chunk of imports
        # Actually, let's just insert it after the `from reportlab...` line.
        content = re.sub(r'(from reportlab[^_]*\n)', r'\1' + import_stmt, content, count=1)

    # 3. Replace instantiation
    # Sometimes it's `styles = _styles()` or `s = _styles()`
    content = re.sub(r'(\w+)\s*=\s*_styles\(\)', rf'\1 = get_unified_styles("{prefix}")', content)

    with open(filepath, 'w') as f:
        f.write(content)

print("Refactor complete.")
