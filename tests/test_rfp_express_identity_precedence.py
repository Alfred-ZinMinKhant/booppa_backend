"""Regression test for RFP Express/Complete identity resolution precedence on reused accounts.

Ensures that when a single user account is reused to generate RFP kits for different
companies (or carries legacy/stale account-level UEN/company data like SPQR Communications),
each kit's vendor context, ACRA resolution, and identity strictly match the specific
report's intake data — for both RFP Express (SGD 129) and RFP Complete (SGD 599).
"""

import uuid
import pytest
from app.core.models import User, MarketplaceVendor
from app.services.rfp_express_builder import RFPExpressBuilder


@pytest.fixture
def reused_user_account(test_db):
    """Create a user account populated with stale/legacy identity data (e.g. SPQR)."""
    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        email=f"qa_reused_{user_id[:8]}@example.com",
        hashed_password="mock_hashed_password",
        company="SPQR COMMUNICATIONS",
        uen="21374700J",
        is_active=True,
    )
    test_db.add(user)
    test_db.commit()

    # Add MarketplaceVendor rows for ACRA lookups
    vendor_spqr = MarketplaceVendor(
        uen="21374700J",
        company_name="SPQR COMMUNICATIONS PTE LTD",
        slug="spqr-communications",
    )
    vendor_netpoleons = MarketplaceVendor(
        uen="201234567A",
        company_name="NETPOLEONS TECHNOLOGY PTE LTD",
        slug="netpoleons-technology",
    )
    vendor_acme = MarketplaceVendor(
        uen="209876543B",
        company_name="ACME SECURITY SOLUTIONS PTE LTD",
        slug="acme-security",
    )
    test_db.add_all([vendor_spqr, vendor_netpoleons, vendor_acme])
    test_db.commit()
    return user


def test_build_vendor_context_identity_precedence(test_db, reused_user_account):
    """Assert intake UEN and company name win over stale User account data."""
    builder = RFPExpressBuilder(
        vendor_id=str(reused_user_account.id),
        vendor_email=reused_user_account.email,
        session_id="cs_test_session_1",
    )

    # 1st run: Company A (Netpoleons)
    intake_a = {
        "uen": "201234567A",
        "company_name": "Netpoleons Technology",
    }
    ctx_a = builder._build_vendor_context(
        company_name="Netpoleons Technology",
        vendor_url="https://netpoleons.example.com",
        db=test_db,
        intake=intake_a,
    )

    assert ctx_a["uen"] == "201234567A"
    assert ctx_a["company_name"] == "Netpoleons Technology"
    assert ctx_a["acra_name"] == "NETPOLEONS TECHNOLOGY PTE LTD"
    assert ctx_a["uen"] != reused_user_account.uen

    # 2nd run: Company B (Acme Security) on the EXACT SAME account right after
    builder_b = RFPExpressBuilder(
        vendor_id=str(reused_user_account.id),
        vendor_email=reused_user_account.email,
        session_id="cs_test_session_2",
    )
    intake_b = {
        "uen": "209876543B",
        "company_name": "Acme Security",
    }
    ctx_b = builder_b._build_vendor_context(
        company_name="Acme Security",
        vendor_url="https://acme.example.com",
        db=test_db,
        intake=intake_b,
    )

    assert ctx_b["uen"] == "209876543B"
    assert ctx_b["company_name"] == "Acme Security"
    assert ctx_b["acra_name"] == "ACME SECURITY SOLUTIONS PTE LTD"
    assert ctx_b["uen"] != "201234567A"
    assert ctx_b["uen"] != reused_user_account.uen
    assert ctx_b["acra_name"] != "SPQR COMMUNICATIONS PTE LTD"


@pytest.mark.asyncio
async def test_generate_express_package_both_products_same_account(test_db, reused_user_account, mocker):
    """Test full package generation for both RFP Express and RFP Complete on same account."""
    # Mock network & AI tasks for pure unit execution
    mocker.patch("app.services.evidence_enricher.fetch_acra_status", return_value={"found": True, "registered_name": "NETPOLEONS TECHNOLOGY PTE LTD", "uen": "201234567A"})
    mocker.patch("app.services.evidence_enricher.fetch_pdpc_enforcement", return_value={"checked": True, "found": False})
    mocker.patch("app.services.evidence_enricher.fetch_ssl_grade", return_value={"checked": True, "grade": "A+"})
    mocker.patch("app.services.evidence_enricher.fetch_domain_reputation", return_value={"checked": True, "flagged": False})
    mocker.patch("app.services.evidence_enricher.fetch_hosting_signals", return_value={"checked": True})
    mocker.patch("app.services.dns_security.fetch_dns_security", return_value={"checked": True})
    mocker.patch.object(RFPExpressBuilder, "_fetch_website_context", return_value={"text": "Website content", "privacy_policy_url": "https://netpoleons.example.com/privacy"})
    mocker.patch.object(RFPExpressBuilder, "_generate_qa", return_value={"essential_qa": {"q1": "a1"}, "complete_qa": {"q1": "a1"}})
    mocker.patch.object(RFPExpressBuilder, "_anchor_to_blockchain", return_value="0x123456")
    mocker.patch.object(RFPExpressBuilder, "_upload_pdf", return_value="https://s3.example.com/pdf")
    mocker.patch.object(RFPExpressBuilder, "_upload_docx", return_value="https://s3.example.com/docx")
    mocker.patch.object(RFPExpressBuilder, "_upload_declaration", return_value="https://s3.example.com/decl")
    mocker.patch.object(RFPExpressBuilder, "_upload_appendix_d", return_value="https://s3.example.com/app_d")
    mocker.patch.object(RFPExpressBuilder, "_send_email", return_value=True)

    # 1. Run RFP Express (SGD 129) for Netpoleons
    builder_express = RFPExpressBuilder(
        vendor_id=str(reused_user_account.id),
        vendor_email=reused_user_account.email,
        session_id="cs_express_netpoleons",
    )
    res_express = await builder_express.generate_express_package(
        vendor_url="https://netpoleons.example.com",
        company_name="Netpoleons Technology",
        rfp_details={"intake": {"uen": "201234567A", "company_name": "Netpoleons Technology"}},
        db=test_db,
        product_type="rfp_express",
        allow_incomplete=True,
    )
    assert res_express["vendor_context"]["uen"] == "201234567A"
    assert res_express["vendor_context"]["company_name"] == "Netpoleons Technology"
    assert res_express["vendor_context"]["uen"] != "21374700J"

    # 2. Run RFP Complete (SGD 599) for Acme Security on same user account
    mocker.patch("app.services.evidence_enricher.fetch_acra_status", return_value={"found": True, "registered_name": "ACME SECURITY SOLUTIONS PTE LTD", "uen": "209876543B"})
    builder_complete = RFPExpressBuilder(
        vendor_id=str(reused_user_account.id),
        vendor_email=reused_user_account.email,
        session_id="cs_complete_acme",
    )
    res_complete = await builder_complete.generate_express_package(
        vendor_url="https://acme.example.com",
        company_name="Acme Security",
        rfp_details={"intake": {"uen": "209876543B", "company_name": "Acme Security"}},
        db=test_db,
        product_type="rfp_complete",
        allow_incomplete=True,
    )
    assert res_complete["vendor_context"]["uen"] == "209876543B"
    assert res_complete["vendor_context"]["company_name"] == "Acme Security"
    assert res_complete["vendor_context"]["uen"] != "201234567A"
    assert res_complete["vendor_context"]["uen"] != "21374700J"
