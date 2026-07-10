import os
import re

directory = 'app'
replacements = {
    r'from app\.core\.models_csp import': 'from app.core.models import',
    r'from app\.core\.models_gebiz import': 'from app.core.models import',
    r'from app\.core\.pdpa_declaration_models import': 'from app.core.models import',
    r'from app\.core\.ropa_models import': 'from app.core.models import'
}

for root, dirs, files in os.walk(directory):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                content = f.read()
            
            modified = False
            for pattern, replacement in replacements.items():
                if re.search(pattern, content):
                    content = re.sub(pattern, replacement, content)
                    modified = True
                    
            if modified:
                with open(path, 'w') as f:
                    f.write(content)
                print(f"Updated imports in {path}")

print("Import fixing complete.")
