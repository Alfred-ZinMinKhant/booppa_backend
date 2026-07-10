import re
import os

files_to_merge = [
    "app/core/models_csp.py",
    "app/core/models_gebiz.py",
    "app/core/pdpa_declaration_models.py",
    "app/core/ropa_models.py"
]

target_file = "app/core/models.py"

with open(target_file, "a") as out:
    for f in files_to_merge:
        with open(f, "r") as src:
            lines = src.readlines()
            
            # Skip imports at the top if they are already in models.py, or just append everything
            # Since models.py likely already has sqlalchemy imports, we can just strip imports 
            # or simply append everything. It's safer to append, but avoid redefining Base.
            
            filtered_lines = []
            for line in lines:
                # Remove imports that might conflict or are redundant, though Python allows re-imports
                if line.startswith("from app.core.db import Base") or line.startswith("from .db import Base"):
                    continue
                filtered_lines.append(line)
            
            out.write("\n\n")
            out.write(f"# --- Merged from {os.path.basename(f)} ---\n")
            out.write("".join(filtered_lines))

print("Models merged successfully.")
