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

            from app.services.email_layout import (
                branded_email_html,
                email_download_card,
                email_info_box,
            )

            product_label = "RFP Kit Complete" if product_type == "rfp_complete" else "RFP Kit Express"

            cards = email_download_card(
                download_url, "Download RFP Kit Evidence (PDF)",
                "Your primary, blockchain-verified compliance evidence certificate.",
                primary=True,
            )
            if docx_url:
                cards += email_download_card(
                    docx_url, "Download Editable Evidence Pack (DOCX)",
                    "An editable Word version of your evidence pack — adapt the wording "
                    "to a specific tender before submission.",
                )
            if declaration_url:
                cards += email_download_card(
                    declaration_url, "Download Supplier Compliance Declaration (PDF)",
                    "Attach this declaration alongside your kit and map each item to the "
                    "matching appendix in your specific tender.",
                )
            if appendix_d_url:
                cards += email_download_card(
                    appendix_d_url, "Download Data Protection Appendix (PDF)",
                    "A generic, reusable data-protection appendix template — renumber its "
                    "items to match the appendix of your specific tender before submission.",
                )

            bc_section = ""
            if blockchain_proof and blockchain_proof.get("verify_url"):
                bc_section = email_info_box(
                    f"<strong>Blockchain verification:</strong><br>"
                    f"<a href=\"{blockchain_proof['verify_url']}\" "
                    f"style=\"color:#10b981;word-break:break-all;\">"
                    f"{blockchain_proof['verify_url']}</a>",
                    tone="success",
                )

            inner = f"""
              <p style="margin:0 0 12px;font-size:15px;line-height:1.6;">Hello,</p>
              <p style="margin:0 0 24px;font-size:15px;line-height:1.6;">
                Your <strong>{product_label}</strong> evidence certificate for
                <strong>{vendor_name}</strong> has been generated and is ready
                for download.</p>
              {cards}
              {bc_section}
              <p style="margin:0 0 20px;font-size:15px;line-height:1.6;">
                The certificate includes blockchain-verified evidence of your PDPA
                compliance posture, ready to attach to any GeBIZ or government RFP
                submission.</p>
              {email_info_box(
                  "These links are valid for 7 days. If you need a fresh copy, log in "
                  "to your Booppa dashboard and re-download from your order history."
              )}
              <p style="margin:0;font-size:15px;line-height:1.6;">
                Thank you for using <strong>BOOPPA</strong>.</p>
            """
            body_html = branded_email_html(
                inner,
                title="Your RFP Kit Evidence is Ready",
                preheader=f"{product_label} certificate for {vendor_name} is ready to download.",
            )

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
