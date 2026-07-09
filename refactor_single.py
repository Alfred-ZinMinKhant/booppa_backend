import re

with open("app/services/fulfillment/single_products.py", "r") as f:
    content = f.read()

# Replace Notarization Certificate
content = re.sub(
    r'body_html = f\"\"\"\s*<html><body style="font-family:Arial,sans-serif;color:#0f172a;">\s*<h2 style="color:#10b981;">Your Notarization Certificate is Ready.*?</body></html>\"\"\"',
    r'from app.services.email_templates import get_notarization_certificate_html\n                body_html = get_notarization_certificate_html(report.company_name, original_filename, file_hash)',
    content,
    flags=re.DOTALL
)

# Replace RFP Kit
content = re.sub(
    r'body_html = f\"\"\"\s*<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">\s*<div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">\s*<h1 style="color:#10b981;margin:0;font-size:20px;">A few details needed to finish your RFP Kit.*?</body></html>\"\"\"',
    r'from app.services.email_templates import get_rfp_kit_needs_info_html\n        body_html = get_rfp_kit_needs_info_html(company_name, fields_html, cta_html)',
    content,
    flags=re.DOTALL
)

# Replace Vendor Proof
content = re.sub(
    r'body_html = f\"\"\"\s*<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">\s*<div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">\s*<h1 style="color:#10b981;margin:0;font-size:20px;">Vendor Proof Activated.*?</body></html>\"\"\"',
    r'from app.services.email_templates import get_vendor_proof_activated_html\n            body_html = get_vendor_proof_activated_html(company_name, vp_score_display, vp_readiness_label, _pdpa_compliance, badge_html, _vp_expires_display, cert_url, bool(cert_pdf), report_id)',
    content,
    flags=re.DOTALL
)

# Replace PDPA Snapshot
content = re.sub(
    r'body_html = f\"\"\"\s*<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">\s*<div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">\s*<h1 style="color:#10b981;margin:0;font-size:20px;">Your PDPA Snapshot is Ready.*?</body></html>\s*\"\"\"',
    r'from app.services.email_templates import get_pdpa_snapshot_ready_html\n                body_html = get_pdpa_snapshot_ready_html(company_name, website_url, _email_compliance, report_id, download_section)',
    content,
    flags=re.DOTALL
)

with open("app/services/fulfillment/single_products.py", "w") as f:
    f.write(content)
