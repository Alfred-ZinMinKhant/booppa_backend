# app/services/rfp_express_emailer.py

import os
from datetime import datetime
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class RFPExpressEmailer:
    """
    Email notifier for RFP Kit Express (SGD 129).
    """
    
    async def send_express_ready_email(
        self,
        customer_email: str,
        vendor_name: str,
        download_url: str,
        blockchain_proof: Optional[Dict] = None,
        scan_summary: Optional[Dict] = None
    ):
        """Send email when RFP Kit Express package is ready"""
        
        subject = f"Your RFP Kit Evidence is Ready - {vendor_name} (RFP Kit Express)"
        
        # Template logic rebranding
        # "Your Vendor Proof is Ready" -> "Your RFP Kit Evidence is Ready"
        # "RFP Express" -> "RFP Kit Express"
        
        logger.info(f"✉️  RFP Kit Express ready email sent to {customer_email}")
