"""
RFP Kit Express — delivery email
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class RFPExpressEmailer:
    """Send the delivery email when an RFP Kit Express package is ready."""

    async def send_express_ready_email(
        self,
        customer_email: str,
        vendor_name: str,
        download_url: str,
        blockchain_proof: Optional[Dict] = None,
        scan_summary: Optional[Dict] = None,
        product_type: str = "rfp_express",
        declaration_url: Optional[str] = None,
        appendix_d_url: Optional[str] = None,
        docx_url: Optional[str] = None,
        pdf_bytes: Optional[bytes] = None,
    ) -> bool:
        try:
            from app.services.email_service import EmailService
            svc = EmailService()

            product_label = "RFP Kit Complete" if product_type == "rfp_complete" else "RFP Kit Express"

            bc_section = ""
            if blockchain_proof and blockchain_proof.get("verify_url"):
                bc_section = (
                    f"<p><strong>Blockchain verification:</strong> "
                    f"<a href=\"{blockchain_proof['verify_url']}\">"
                    f"{blockchain_proof['verify_url']}</a></p>"
                )

            decl_section = ""
            if declaration_url:
                decl_section = (
                    f"<p><a href=\"{declaration_url}\" "
                    f"style=\"background:#0f172a;color:#fff;padding:10px 22px;"
                    f"text-decoration:none;border-radius:6px;font-weight:bold;\">"
                    f"Download Supplier Compliance Declaration (PDF)</a></p>"
                    f"<p style=\"color:#64748b;font-size:12px;\">Attach this declaration "
                    f"alongside your kit and map each item to the matching appendix in "
                    f"your specific tender.</p>"
                )

            docx_section = ""
            if docx_url:
                docx_section = (
                    f"<p><a href=\"{docx_url}\" "
                    f"style=\"background:#0f172a;color:#fff;padding:10px 22px;"
                    f"text-decoration:none;border-radius:6px;font-weight:bold;\">"
                    f"Download Editable Evidence Pack (DOCX)</a></p>"
                    f"<p style=\"color:#64748b;font-size:12px;\">An editable Word "
                    f"version of your evidence pack — adapt the wording to a specific "
                    f"tender before submission.</p>"
                )

            apx_section = ""
            if appendix_d_url:
                apx_section = (
                    f"<p><a href=\"{appendix_d_url}\" "
                    f"style=\"background:#0f172a;color:#fff;padding:10px 22px;"
                    f"text-decoration:none;border-radius:6px;font-weight:bold;\">"
                    f"Download Data Protection Appendix (PDF)</a></p>"
                    f"<p style=\"color:#64748b;font-size:12px;\">A generic, reusable "
                    f"data-protection appendix template — renumber its items to match the "
                    f"appendix of your specific tender before submission.</p>"
                )

            body_html = f"""
            <html><body style="font-family:Arial,sans-serif;color:#0f172a;">
              <h2 style="color:#10b981;">Your RFP Kit Evidence is Ready</h2>
              <p>Hello,</p>
              <p>Your <strong>{product_label}</strong> evidence certificate for
                 <strong>{vendor_name}</strong> has been generated and is ready for download.</p>
              <p>
                <a href="{download_url}"
                   style="background:#10b981;color:#fff;padding:12px 24px;
                          text-decoration:none;border-radius:6px;font-weight:bold;">
                  Download RFP Kit Evidence (PDF)
                </a>
              </p>
              {bc_section}
              {docx_section}
              {decl_section}
              {apx_section}
              <p>The certificate includes blockchain-verified evidence of your PDPA
                 compliance posture, ready to attach to any GeBIZ or government RFP
                 submission.</p>
              <p style="color:#64748b;font-size:12px;">
                This link is valid for 7 days. If you need a fresh copy, log in to
                your Booppa dashboard and re-download from your order history.
              </p>
              <p>Thank you for using BOOPPA.</p>
            </body></html>
            """

            _attachments = None
            if pdf_bytes:
                _safe_co = (vendor_name or "Kit").replace("/", "-").replace(" ", "-")
                _filename = "RFP_Complete_Kit" if product_type == "rfp_complete" else "RFP_Kit_Express"
                _attachments = [(f"{_filename}_{_safe_co}.pdf", pdf_bytes)]

            await svc.send_html_email(
                to_email=customer_email,
                subject=f"Your {product_label} Evidence is Ready — {vendor_name}",
                body_html=body_html,
                attachments=_attachments,
            )
            logger.info(f"{product_label} delivery email sent to {customer_email}")
            return True
        except Exception as e:
            logger.error(f"RFP Express email failed for {customer_email}: {e}")
            return False
