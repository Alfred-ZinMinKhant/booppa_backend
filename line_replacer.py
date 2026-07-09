def replace_html(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Notarization
        if 'body_html = f"""' in line and '<html><body' in lines[i+1] and 'Your Notarization Certificate is Ready' in lines[i+2]:
            new_lines.append('                from app.services.email_templates import get_notarization_certificate_html\n')
            new_lines.append('                body_html = get_notarization_certificate_html(report.company_name, original_filename, file_hash)\n')
            while '</body></html>"""' not in lines[i]:
                i += 1
            i += 1
            continue

        # RFP Kit
        if 'body_html = f"""' in line and '<html><body' in lines[i+1] and 'A few details needed to finish your RFP Kit' in lines[i+3]:
            new_lines.append('        from app.services.email_templates import get_rfp_kit_needs_info_html\n')
            new_lines.append('        body_html = get_rfp_kit_needs_info_html(company_name, fields_html, cta_html)\n')
            while '</body></html>"""' not in lines[i]:
                i += 1
            i += 1
            continue
            
        # Vendor Proof
        if 'body_html = f"""' in line and '<html><body' in lines[i+1] and 'Vendor Proof Activated' in lines[i+3]:
            new_lines.append('            from app.services.email_templates import get_vendor_proof_activated_html\n')
            new_lines.append('            body_html = get_vendor_proof_activated_html(company_name, vp_score_display, vp_readiness_label, _pdpa_compliance, badge_html, _vp_expires_display, cert_url, bool(cert_pdf), report_id)\n')
            while '</body></html>"""' not in lines[i]:
                i += 1
            i += 1
            continue

        # PDPA Snapshot
        if 'body_html = f"""' in line and '<html><body' in lines[i+1] and 'Your PDPA Snapshot is Ready' in lines[i+3]:
            new_lines.append('                from app.services.email_templates import get_pdpa_snapshot_ready_html\n')
            new_lines.append('                body_html = get_pdpa_snapshot_ready_html(company_name, website_url, _email_compliance, report_id, download_section)\n')
            while '</body></html>' not in lines[i]:
                i += 1
            i += 2
            continue

        new_lines.append(line)
        i += 1

    with open(filepath, 'w') as f:
        f.writelines(new_lines)

replace_html("app/services/fulfillment/single_products.py")
