with open("app/services/fulfillment/subscriptions.py", "r") as f:
    sub_lines = f.readlines()

# Extract lines 150 to end (0-indexed 149 to end)
orphan_lines = sub_lines[149:]
remaining_sub_lines = sub_lines[:149]

with open("app/services/email_templates.py", "r") as f:
    email_lines = f.readlines()

# Append orphan lines to email_templates
with open("app/services/email_templates.py", "w") as f:
    f.writelines(email_lines)
    f.writelines(orphan_lines)

# Write back remaining lines to subscriptions.py
with open("app/services/fulfillment/subscriptions.py", "w") as f:
    f.writelines(remaining_sub_lines)
