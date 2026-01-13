#!/usr/bin/env python3
"""
Test Booppa AI Integration (copied into booppa_v10_enterprise for container access)
"""

import asyncio
import sys
import os

# Ensure project root is on path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.booppa_ai_service import BooppaAIService


async def test_integration():
    ai_service = BooppaAIService()
    test_scan_data = {
        "company_name": "SG Retail Pte Ltd",
        "url": "http://insecure.example",
        "collects_nric": True,
        "has_legal_justification": False,
        "uses_https": False,
        "consent_mechanism": {"has_cookie_banner": False, "has_active_consent": False},
        "dpo_compliance": {"has_dpo": False},
        "dnc_mention": {"mentions_dnc": False},
    }

    report = await ai_service.generate_compliance_report(test_scan_data)
    print("Report ID:", report["report_metadata"]["report_id"])  # minimal verification


if __name__ == "__main__":
    asyncio.run(test_integration())
