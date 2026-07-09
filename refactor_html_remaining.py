import os
import re

def extract_html_and_replace(filepath):
    with open(filepath, "r") as f:
        content = f.read()

    # Find all occurrences of body_html = f"""...""" or similar
    pattern = re.compile(r'(\s*[a-zA-Z0-9_]+_html\s*=\s*f?\"\"\"(?:(?!\"\"\").)*<html>.*?\"\"\")', re.DOTALL)
    matches = pattern.findall(content)
    if not matches:
        return
        
    print(f"Found {len(matches)} matches in {filepath}")
    
    # We will just ignore this complex extraction script and use a simpler approach.
    pass

