import re

with open("app/services/pdf_service.py", "r") as f:
    lines = f.readlines()

# Find class PDFService:
class_idx = -1
for i, line in enumerate(lines):
    if line.startswith("class PDFService:"):
        class_idx = i
        break

# Find def get_booppa_styles() -> dict:
func_idx = -1
for i, line in enumerate(lines):
    if line.startswith("def get_booppa_styles() -> dict:"):
        func_idx = i
        break

# Find the end of get_booppa_styles (next def _section_header)
end_idx = -1
for i in range(func_idx + 1, len(lines)):
    if line.startswith("    def _section_header(self, title: str):") or "def _section_header" in lines[i]:
        end_idx = i
        break

# We will move the get_booppa_styles block to just above class_idx
if class_idx != -1 and func_idx != -1 and end_idx != -1 and func_idx > class_idx:
    func_block = lines[func_idx:end_idx]
    # Remove from original position
    lines = lines[:func_idx] + lines[end_idx:]
    # Insert before class
    lines = lines[:class_idx] + func_block + lines[class_idx:]
    
    with open("app/services/pdf_service.py", "w") as f:
        f.writelines(lines)
    print("Fixed PDFService scoping")
else:
    print(f"Could not find indices: class={class_idx}, func={func_idx}, end={end_idx}")

