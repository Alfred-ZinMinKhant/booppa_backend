def get_vendor_active_no_website_html() -> str:
    return """<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f8fafc;padding:24px;">
    <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:8px;padding:28px;border:1px solid #e2e8f0;">
      <h2 style="margin:0 0 12px;color:#0f172a;">Welcome to Compliance Evidence — one more step</h2>
      <p style="color:#334155;line-height:1.55;">
        Your subscription is active, but we need your website on file to generate
        your PDPA Snapshot, RFP Complete Kit, and monthly Cover Sheet.
      </p>
      <a href="https://www.booppa.io/vendor/profile"
         style="background:#0f172a;color:#fff;padding:10px 20px;text-decoration:none;
                border-radius:6px;font-weight:bold;display:inline-block;margin-top:12px;">
        Add your website
      </a>
      <p style="margin-top:24px;font-size:11px;color:#94a3b8;">
        Once saved, your first cycle will run automatically.
      </p>
    </div></body></html>"""

def get_vendor_suite_onboarding_html(suite_label: str, features_html: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
    <div style="max-width:600px;margin:0 auto;">
      <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
        <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
        <h1 style="margin:0;color:#10b981;font-size:22px;">{suite_label} — you're all set</h1>
      </div>
      <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
        <p style="color:#cbd5e1;line-height:1.6;margin:0 0 18px;">
          Your <strong>{suite_label}</strong> subscription is now active. Here's everything it unlocks and where to start:
        </p>
        <table style="width:100%;border-collapse:collapse;">{features_html}</table>
        <div style="text-align:center;margin:26px 0 6px;">
          <a href="https://www.booppa.io/vendor/dashboard" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
        </div>
        <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
      </div>
    </div></body></html>"""

def get_buyer_suite_onboarding_html(buyer_label: str, seats_txt: str, dash_url: str, features_html: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
    <div style="max-width:600px;margin:0 auto;">
      <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
        <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
        <h1 style="margin:0;color:#10b981;font-size:22px;">{buyer_label} — you're all set</h1>
      </div>
      <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
        <p style="color:#cbd5e1;line-height:1.6;margin:0 0 8px;">
          Your <strong>{buyer_label}</strong> subscription is now active — {seats_txt}. Here's everything it unlocks and where to start:
        </p>
        <table style="width:100%;border-collapse:collapse;">{features_html}</table>
        <div style="text-align:center;margin:26px 0 6px;">
          <a href="{dash_url}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
        </div>
        <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
      </div>
    </div></body></html>"""

def get_evidence_pack_intake_html(intake_url: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:620px;margin:0 auto;">
          <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
            <h1 style="color:#10b981;margin:0;font-size:20px;">Start your PDPA Compliance Evidence Pack</h1>
          </div>
          <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
            <p>Thank you for your purchase. Your Evidence Pack builds seven PDPA governance
               documents — DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook,
               Training Register, and Security Review Log — tailored to your organisation.</p>
            <p>To generate documents that reflect your actual operations, we need a short
               structured intake (about 5 minutes): your org details, DPO, systems, data types,
               and where data is hosted.</p>
            <div style="text-align:center;margin:24px 0;">
              <a href="{intake_url}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Complete your intake →</a>
            </div>
            <p style="color:#64748b;font-size:12px;">Every document is an AI-generated DRAFT with no
               evidentiary value until your authorised representative reviews and signs it.</p>
          </div>
        </body></html>"""

import html as _html
from datetime import datetime, timezone

def get_notarization_certificate_html(company_name: str, original_filename: str, file_hash: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;color:#0f172a;">
                  <h2 style="color:#10b981;">Your Notarization Certificate is Ready</h2>
                  <p>Hello {company_name or "Customer"},</p>
                  <p>Your blockchain notarization certificate for
                     <strong>{original_filename}</strong> has been generated.</p>
                  <p><strong>SHA-256 Hash:</strong> <code>{file_hash}</code></p>
               </body></html>"""

def get_rfp_kit_needs_info_html(company_name: str, fields_html: str, cta_html: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
          <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
            <h1 style="color:#10b981;margin:0;font-size:20px;">A few details needed to finish your RFP Kit</h1>
          </div>
          <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
            <p>Hello <strong>{_html.escape(company_name or "there")}</strong>,</p>
            <p>Your RFP Complete Kit is almost ready. To make it usable for a real GeBIZ tender, we
               need you to confirm a few verification details we could not source automatically.
               Your kit will be generated and delivered as soon as you complete these:</p>
            <ul style="font-size:14px;color:#334155;padding-left:20px;">{fields_html}</ul>
            {cta_html}
            <p style="color:#64748b;font-size:12px;">We don't deliver kits with unverified
               placeholders — GeBIZ procurement officers reject them. This step keeps yours submission-ready.</p>
          </div>
        </body></html>"""

def get_vendor_proof_activated_html(company_name: str, vp_score_display: str, vp_readiness_label: str, _pdpa_compliance: str | None, badge_html: str, _vp_expires_display: str, cert_url: str | None, cert_pdf: bool, report_id: str) -> str:
    pdpa_text = "(run a PDPA scan to establish it)" if _pdpa_compliance is None else ""
    cert_text = ("<p style='margin-top:20px;color:#475569;font-size:13px;'>Your Vendor Proof certificate (valid until " + _vp_expires_display + ") is <strong>attached to this email as a PDF</strong>." + ("<br>You can also <a href='" + cert_url + "'>download it here</a>." if cert_url else "") + "</p>") if cert_pdf else ""
    return f"""<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
              <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                <h1 style="color:#10b981;margin:0;font-size:20px;">Vendor Proof Activated</h1>
              </div>
              <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                <p>Hello <strong>{company_name}</strong>,</p>
                <p>Your Vendor Proof is now <strong style="color:#10b981;">active</strong>. You are now visible to procurement officers who filter by verified vendors on the BOOPPA platform.</p>
                <h3 style="color:#0f172a;">What changed on your profile</h3>
                <ul>
                  <li>Verification status: <strong>BASIC (Identity Verified, Active)</strong></li>
                  <li>Compliance score: <strong>{vp_score_display}</strong></li>
                  <li>Procurement readiness: <strong>{vp_readiness_label}</strong></li>
                  <li>CAL Level 1 activated — personalised upgrade recommendations will appear in your dashboard</li>
                </ul>
                <p style="color:#475569;font-size:13px;background:#f8fafc;border-left:3px solid #94a3b8;padding:10px 14px;border-radius:4px;">
                  <strong>What Vendor Proof attests:</strong> your identity and registration on BOOPPA — not a
                  compliance endorsement. Your procurement readiness above reflects your latest PDPA scan
                  {pdpa_text}. Procurement officers
                  see your real standing on your verification page.
                </p>
                <h3 style="color:#0f172a;">Embed your Booppa Verified badge</h3>
                <p>Add this to your website or RFP proposals:</p>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-family:monospace;font-size:12px;word-break:break-all;">
                  {badge_html.replace('<', '&lt;').replace('>', '&gt;')}
                </div>
                <div style="margin-top:16px;">{badge_html}</div>
                {cert_text}
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/vendor/dashboard" style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
                    Go to Dashboard →
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;margin-top:24px;">
                  Verification ID: {report_id}<br>
                  Verified on: {datetime.now(timezone.utc).strftime('%d %B %Y')}<br>
                  booppa.io
                </p>
              </div>
            </body></html>"""

def get_pdpa_snapshot_ready_html(company_name: str, website_url: str, _email_compliance: int, report_id: str, download_section: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                    <h1 style="color:#10b981;margin:0;font-size:20px;">Your PDPA Snapshot is Ready</h1>
                  </div>
                  <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                    <p>Hello <strong>{company_name}</strong>,</p>
                    <p>Your PDPA Snapshot report for <strong>{website_url}</strong> has been generated.</p>
                    <p>The report evaluates your compliance across 8 PDPA dimensions — consent, data flow,
                       DSAR procedures, breach notification, retention, third-party processors, DPO, and
                       privacy notice — and provides specific recommendations with legislative references.</p>
                    <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;
                                border-radius:4px;margin:20px 0;">
                      <strong>Compliance Score:</strong> {_email_compliance}/100<br>
                      <strong>Report ID:</strong> {report_id[:8].upper()}<br>
                      <strong>Generated:</strong> {datetime.now(timezone.utc).strftime('%d %B %Y')}
                    </div>
                    <p>Your compliance score on BOOPPA has been updated to reflect this scan.
                       Procurement officers searching for verified vendors will see your improved standing.</p>
                    {download_section}
                    <p style="margin-top:24px;">
                      <a href="https://www.booppa.io/vendor/dashboard"
                         style="color:#10b981;text-decoration:underline;">View your dashboard →</a>
                    </p>
                    <p style="color:#64748b;font-size:11px;margin-top:24px;">
                      This report is for informational purposes only and does not constitute legal advice
                      or PDPC certification. BOOPPA is not a law firm.
                    </p>
                  </div>
                </body></html>"""
