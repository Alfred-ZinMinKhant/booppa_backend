import os
import re

directory = 'app/api'
for root, dirs, files in os.walk(directory):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                content = f.read()
            
            if 'APIRouter(' in content and 'RetryAPIRoute' not in content:
                content = "from app.core.route_classes import RetryAPIRoute\n" + content
                content = re.sub(
                    r'APIRouter\((.*?)\)',
                    lambda m: f"APIRouter({m.group(1)}{', ' if m.group(1).strip() else ''}route_class=RetryAPIRoute)",
                    content
                )
                with open(path, 'w') as f:
                    f.write(content)
                print(f"Updated {path}")
