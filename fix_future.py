import glob

for filepath in glob.glob("app/services/**/*.py", recursive=True):
    with open(filepath, "r") as f:
        lines = f.readlines()
        
    future_line = None
    for i, line in enumerate(lines):
        if line.startswith("from __future__ import"):
            future_line = line
            lines.pop(i)
            break
            
    if future_line:
        # insert after docstring or at the top
        insert_idx = 0
        if lines[0].startswith('"""'):
            for i in range(1, len(lines)):
                if lines[i].startswith('"""'):
                    insert_idx = i + 1
                    break
        elif lines[0].startswith("'''"):
            for i in range(1, len(lines)):
                if lines[i].startswith("'''"):
                    insert_idx = i + 1
                    break
                    
        lines.insert(insert_idx, future_line)
        
        with open(filepath, "w") as f:
            f.writelines(lines)
        print(f"Fixed {filepath}")
