import html as _html
from datetime import datetime, timezone

from app.services.email_layout import (
    branded_email_html,
    email_button,
    email_info_box,
    email_kv,
)

# Shared inline paragraph style so bodies read consistently inside the white card.
_P = 'style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#334155;"'


def get_vendor_active_no_website_html() -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Welcome to Compliance Evidence — one more step</h2>
      <p {_P}>
        Your subscription is active, but we need your website on file to generate
        your PDPA Snapshot, RFP Complete Kit, and monthly Cover Sheet.
      </p>
      {email_button("https://www.booppa.io/vendor/profile", "Add your website")}
      {email_info_box("Once saved, your first cycle will run automatically.")}
    """
    return branded_email_html(
        inner,
        title="One more step to start",
        preheader="Add your website to start your first compliance cycle.",
    )


def get_vendor_suite_onboarding_html(suite_label: str, features_html: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">{suite_label} — you're all set</h2>
      <p {_P}>
        Your <strong>{suite_label}</strong> subscription is now active. Here's everything
        it unlocks and where to start:
      </p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;margin:0 0 22px;">{features_html}</table>
      {email_button("https://www.booppa.io/vendor/dashboard", "Go to your dashboard →")}
      <p style="color:#64748b;font-size:12px;margin:16px 0 0;">Questions? Reply to this email or visit booppa.io/support.</p>
    """
    return branded_email_html(
        inner,
        title="Subscription active",
        preheader=f"Your {suite_label} subscription is now active.",
    )


def get_buyer_suite_onboarding_html(buyer_label: str, seats_txt: str, dash_url: str, features_html: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">{buyer_label} — you're all set</h2>
      <p {_P}>
        Your <strong>{buyer_label}</strong> subscription is now active — {seats_txt}. Here's
        everything it unlocks and where to start:
      </p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;margin:0 0 22px;">{features_html}</table>
      {email_button(dash_url, "Go to your dashboard →")}
      <p style="color:#64748b;font-size:12px;margin:16px 0 0;">Questions? Reply to this email or visit booppa.io/support.</p>
    """
    return branded_email_html(
        inner,
        title="Subscription active",
        preheader=f"Your {buyer_label} subscription is now active.",
    )


def get_evidence_pack_intake_html(intake_url: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Start your PDPA Compliance Evidence Pack</h2>
      <p {_P}>Thank you for your purchase. Your Evidence Pack builds seven PDPA governance
         documents — DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook,
         Training Register, and Security Review Log — tailored to your organisation.</p>
      <p {_P}>To generate documents that reflect your actual operations, we need a short
         structured intake (about 5 minutes): your org details, DPO, systems, data types,
         and where data is hosted.</p>
      {email_button(intake_url, "Complete your intake →")}
      <p style="color:#64748b;font-size:12px;margin:0;">Every document is an AI-generated DRAFT with no
         evidentiary value until your authorised representative reviews and signs it.</p>
    """
    return branded_email_html(
        inner,
        title="Complete your intake",
        preheader="A short 5-minute intake to generate your Evidence Pack.",
    )


def get_notarization_certificate_html(company_name: str, original_filename: str, file_hash: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Notarization Certificate is Ready</h2>
      <p {_P}>Hello {_html.escape(company_name or "Customer")},</p>
      <p {_P}>Your blockchain notarization certificate for
         <strong>{_html.escape(original_filename or "")}</strong> has been generated.</p>
      {email_kv([("SHA-256 Hash", f'<code>{_html.escape(file_hash or "")}</code>')])}
    """
    return branded_email_html(
        inner,
        title="Notarization certificate ready",
        preheader="Your blockchain notarization certificate has been generated.",
    )


def get_rfp_kit_needs_info_html(company_name: str, fields_html: str, cta_html: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">A few details needed to finish your RFP Kit</h2>
      <p {_P}>Hello <strong>{_html.escape(company_name or "there")}</strong>,</p>
      <p {_P}>Your RFP Complete Kit is almost ready. To make it usable for a real GeBIZ tender, we
         need you to confirm a few verification details we could not source automatically.
         Your kit will be generated and delivered as soon as you complete these:</p>
      <ul style="font-size:14px;color:#334155;line-height:1.6;padding-left:20px;margin:0 0 20px;">{fields_html}</ul>
      {cta_html}
      <p style="color:#64748b;font-size:12px;margin:0;">We don't deliver kits with unverified
         placeholders — GeBIZ procurement officers reject them. This step keeps yours submission-ready.</p>
    """
    return branded_email_html(
        inner,
        title="A few details needed",
        preheader="Confirm a few verification details to finish your RFP Kit.",
    )


def get_vendor_proof_activated_html(company_name: str, vp_score_display: str, vp_readiness_label: str, _pdpa_compliance: str | None, badge_html: str, _vp_expires_display: str, cert_url: str | None, cert_pdf: bool, report_id: str) -> str:
    pdpa_text = "(run a PDPA scan to establish it)" if _pdpa_compliance is None else ""
    cert_text = ("<p style='margin:20px 0 0;color:#475569;font-size:13px;'>Your Vendor Proof certificate (valid until " + _vp_expires_display + ") is <strong>attached to this email as a PDF</strong>." + ("<br>You can also <a href='" + cert_url + "'>download it here</a>." if cert_url else "") + "</p>") if cert_pdf else ""
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Vendor Proof Activated</h2>
      <p {_P}>Hello <strong>{_html.escape(company_name or "there")}</strong>,</p>
      <p {_P}>Your Vendor Proof is now <strong style="color:#10b981;">active</strong>. You are now visible to procurement officers who filter by verified vendors on the BOOPPA platform.</p>
      <h3 style="color:#0f172a;font-size:16px;margin:0 0 8px;">What changed on your profile</h3>
      <ul style="font-size:14px;color:#334155;line-height:1.6;padding-left:20px;margin:0 0 16px;">
        <li>Verification status: <strong>BASIC (Identity Verified, Active)</strong></li>
        <li>Compliance score: <strong>{vp_score_display}</strong></li>
        <li>Procurement readiness: <strong>{vp_readiness_label}</strong></li>
        <li>Commercial Activation Layer live — personalised upgrade recommendations are in your dashboard</li>
      </ul>
      {email_info_box(
          "<strong>What Vendor Proof attests:</strong> your identity and registration on BOOPPA — not a "
          "compliance endorsement. Your procurement readiness above reflects your latest PDPA scan "
          f"{pdpa_text}. Procurement officers see your real standing on your verification page."
      )}
      <h3 style="color:#0f172a;font-size:16px;margin:0 0 8px;">Embed your Booppa Verified badge</h3>
      <p {_P}>Add this to your website or RFP proposals:</p>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-family:monospace;font-size:12px;word-break:break-all;">
        {badge_html.replace('<', '&lt;').replace('>', '&gt;')}
      </div>
      <div style="margin-top:16px;">{badge_html}</div>
      {cert_text}
      <div style="margin:24px 0 0;">{email_button("https://www.booppa.io/vendor/dashboard", "Go to Dashboard →")}</div>
      <p style="color:#64748b;font-size:12px;margin:8px 0 0;">
        Verification ID: {report_id}<br>
        Verified on: {datetime.now(timezone.utc).strftime('%d %B %Y')}<br>
        booppa.io
      </p>
    """
    return branded_email_html(
        inner,
        title="Vendor Proof activated",
        preheader="Your Vendor Proof is now active and visible to procurement officers.",
    )


def get_pdpa_snapshot_ready_html(company_name: str, website_url: str, _email_compliance: int, report_id: str, download_section: str) -> str:
    inner = f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your PDPA Snapshot is Ready</h2>
      <p {_P}>Hello <strong>{_html.escape(company_name or "there")}</strong>,</p>
      <p {_P}>Your PDPA Snapshot report for <strong>{_html.escape(website_url or "")}</strong> has been generated.</p>
      <p {_P}>The report evaluates your compliance across 8 PDPA dimensions — consent, data flow,
         DSAR procedures, breach notification, retention, third-party processors, DPO, and
         privacy notice — and provides specific recommendations with legislative references.</p>
      {email_kv([
          ("Compliance Score", f"{_email_compliance}/100"),
          ("Report ID", report_id[:8].upper()),
          ("Generated", datetime.now(timezone.utc).strftime('%d %B %Y')),
      ])}
      <p {_P}>Your compliance score on BOOPPA has been updated to reflect this scan.
         Procurement officers searching for verified vendors will see your improved standing.</p>
      {download_section}
      <p style="margin:24px 0 0;">
        <a href="https://www.booppa.io/vendor/dashboard"
           style="color:#10b981;text-decoration:underline;">View your dashboard →</a>
      </p>
      <p style="color:#64748b;font-size:11px;margin:24px 0 0;">
        This report is for informational purposes only and does not constitute legal advice
        or PDPC certification. BOOPPA is not a law firm.
      </p>
    """
    return branded_email_html(
        inner,
        title="PDPA Snapshot ready",
        preheader=f"Your PDPA Snapshot report for {website_url} has been generated.",
    )
