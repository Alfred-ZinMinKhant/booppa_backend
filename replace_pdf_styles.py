import os
import glob

def refactor_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    if "getSampleStyleSheet" not in content and "pdf_styles" not in content:
        return

    # Skip pdf_styles.py itself
    if "pdf_styles.py" in filepath:
        return

    # Add the new import if it doesn't exist
    if "from app.services.pdf_styles import get_unified_styles" not in content:
        # Find where reportlab is imported and add our import
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if "import" in line:
                lines.insert(i, "from app.services.pdf_styles import get_unified_styles")
                break
        content = '\n'.join(lines)

    # Replace the calls
    content = content.replace("getSampleStyleSheet()", "get_unified_styles()")
    
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"Refactored {filepath}")

for filepath in glob.glob("app/services/**/*.py", recursive=True):
    refactor_file(filepath)
