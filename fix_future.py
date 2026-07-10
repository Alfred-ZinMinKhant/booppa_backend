import os
import re

directory = 'app/api'
for root, dirs, files in os.walk(directory):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                content = f.read()
            
            if 'from __future__ import annotations' in content:
                # Remove it and put it at the very top
                content = re.sub(r'^from __future__ import annotations\n', '', content, flags=re.MULTILINE)
                content = "from __future__ import annotations\n" + content
                
                with open(path, 'w') as f:
                    f.write(content)
                print(f"Fixed {path}")

