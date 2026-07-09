import os
import re

target_dir = "app"
count = 0

for root, _, files in os.walk(target_dir):
    for f in files:
        if f.endswith(".py") and f != "models_unified.py":
            filepath = os.path.join(root, f)
            with open(filepath, "r") as file:
                content = file.read()
            
            # Match from app.core.models_xxx import
            # Or import app.core.models_xxx
            new_content = re.sub(r'app\.core\.models_[a-zA-Z0-9_]+', 'app.core.models', content)
            
            if new_content != content:
                with open(filepath, "w") as file:
                    file.write(new_content)
                count += 1
                print(f"Updated {filepath}")

print(f"Total files updated: {count}")
