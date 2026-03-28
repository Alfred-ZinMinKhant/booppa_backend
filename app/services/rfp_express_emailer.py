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

            await svc.send_html_email(
                to_email=customer_email,
                subject=f"Your {product_label} Evidence is Ready — {vendor_name}",
                body_html=body_html,
            )
            logger.info(f"{product_label} delivery email sent to {customer_email}")
            return True
        except Exception as e:
            logger.error(f"RFP Express email failed for {customer_email}: {e}")
            return False
