import os
import re

models_dir = "app/core"
model_files = [
    "models.py",
    "models_csp.py",
    "models_enterprise.py",
    "models_gebiz.py",
    "models_v10.py",
    "models_v11.py",
    "models_v12.py",
    "models_v13.py",
    "models_v6.py",
    "models_v8.py",
    "models_vendor_pro.py"
]

all_content = []

for mf in model_files:
    path = os.path.join(models_dir, mf)
    if not os.path.exists(path):
        continue
    
    with open(path, "r") as f:
        lines = f.readlines()
        
    filtered_lines = []
    for line in lines:
        # Remove cross-imports between model files!
        if re.match(r'^\s*from\s+app\.core\.models[_a-z0-9]*\s+import\s+', line):
            # Skip cross-imports completely
            continue
        filtered_lines.append(line)
        
    all_content.append(f"\n\n# {'='*60}\n# Extracted from {mf}\n# {'='*60}\n")
    all_content.extend(filtered_lines)

# Write to a single temp file
with open("app/core/models_unified.py", "w") as f:
    f.writelines(all_content)

print("Merged all models into app/core/models_unified.py")
