import os
import re

directory = 'app'
for root, dirs, files in os.walk(directory):
    for file in files:
        if file.endswith('.py') and file != 'pdf_styles.py':
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                content = f.read()
            
            modified = False
            
            # Replace inline _styles() method definitions that just wrap getSampleStyleSheet
            # Actually, just replacing getSampleStyleSheet call is easier
            
            if 'getSampleStyleSheet' in content:
                # Import get_unified_styles if not there
                if 'from app.services.pdf_styles import get_unified_styles' not in content:
                    content = "from app.services.pdf_styles import get_unified_styles\n" + content
                
                content = re.sub(r'getSampleStyleSheet\(\)', r'get_unified_styles()', content)
                modified = True
            
            if modified:
                with open(path, 'w') as f:
                    f.write(content)
                print(f"Updated styles in {path}")

print("Style replacement complete.")
