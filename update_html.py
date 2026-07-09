with open('app/services/fulfillment/subscriptions.py', 'r') as f:
    subs_content = f.read()

# We will move _csp_activation_email_html
import re

match = re.search(r'def _csp_activation_email_html.*?return f"""(.*?)"""', subs_content, re.DOTALL)
if match:
    func_text = match.group(0)
    subs_content = subs_content.replace(func_text, "")
    
    with open('app/services/email_templates.py', 'w') as f:
        f.write(func_text + "\n")
        
    with open('app/services/fulfillment/subscriptions.py', 'w') as f:
        # Add import at top
        subs_content = subs_content.replace("import uuid", "import uuid\nfrom app.services.email_templates import _csp_activation_email_html")
        f.write(subs_content)

