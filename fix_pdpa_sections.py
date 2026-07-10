import os
import re

directory = 'app'
pattern = re.compile(r'PDPA Section (\d+(?:\(\d+\))?)')
replacement = r'PDPA 2012 s.\1'

for root, dirs, files in os.walk(directory):
    for file in files:
        if file.endswith('.py') or file.endswith('.json'):
            path = os.path.join(root, file)
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'PDPA Section' in content:
                new_content = pattern.sub(replacement, content)
                if new_content != content:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"Updated {path}")
