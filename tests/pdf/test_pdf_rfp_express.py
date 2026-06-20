"""RFP Express + Complete PDF builder — hermetic test with all external
network dependencies mocked.

What we mock:
  - evidence_enricher.{fetch_acra_status, fetch_pdpc_enforcement,
    fetch_ssl_grade, fetch_domain_reputation, fetch_hosting_signals}
  - RFPExpressBuilder._fetch_website_context  (BeautifulSoup + httpx scrape)
  - BooppaAIService._call_deepseek  (LLM call)
  - BlockchainService              (Polygon RPC)

What we DON'T mock:
  - PDFService.generate_pdf — exercised for real, output parsed with pypdf
  - EmailService.send_html_email — captured via existing `email_capture` fixture
  - S3Service.upload_pdf — runs against moto s3 (via `s3_bucket` fixture)
"""
from io import BytesIO

import pytest

from pypdf import PdfReader


# ── Canned data the mocks return ─────────────────────────────────────────────

_ACRA_OK = {
    "found": True,
    "registered_name": "ACME PRIVATE LIMITED",
    "entity_type": "LOCAL COMPANY",
    "entity_status": "Live Company",
    "live": True,
}
_PDPC_CLEAN  = {"checked": True, "found": False}
_SSL_OK      = {"checked": True, "grade": "A+", "protocols": ["TLSv1.3", "TLSv1.2"]}
_DOMAIN_CLEAN = {"checked": True, "flagged": False, "malicious_votes": 0, "reputation": 0}
_HOSTING_OK  = {
    "checked": True,
    "inferred_provider": "AWS",
    "inferred_region": "ap-southeast-1",
}
_WEBSITE_CTX = {
    "text": "Acme Pte Ltd is a Singapore SaaS provider serving enterprise clients.",
    "privacy_policy_url": "https://acme.test/privacy",
    "is_spa": False,
    "spa_warning": None,
}
_AI_RESPONSE_5Q = (
    '{"data_policy": "We follow PDPA strictly.",'
    ' "dpo_appointed": "Yes, DPO appointed.",'
    ' "security_measures": "Encryption + MFA.",'
    ' "breach_history": "No incidents.",'
    ' "third_party": "DPAs in place."}'
)
_AI_RESPONSE_15Q = (
    '{"data_policy": "We follow PDPA strictly.",'
    ' "dpo_appointed": "Yes, DPO appointed.",'
    ' "security_measures": "Encryption + MFA.",'
    ' "breach_history": "No incidents.",'
    ' "third_party": "DPAs in place.",'
    ' "iso_certifications": "ISO 27001 certified.",'
    ' "business_continuity": "BCP tested annually.",'
    ' "staff_training": "Annual training.",'
    ' "access_controls": "RBAC with MFA.",'
    ' "vulnerability_mgmt": "30-day patch SLA.",'
    ' "encryption_standards": "AES-256 + TLS 1.2+.",'
    ' "audit_logging": "12-month retention.",'
    ' "incident_response": "Documented IR plan.",'
    ' "data_residency": "Singapore-only storage.",'
    ' "subcontracting": "No offshore processing."}'
)
_FAKE_TX_HASH = "0x" + "a" * 64


@pytest.fixture
def _rfp_mocks(mocker):
    """Patch all external dependencies of the RFP builder.

    Note: the evidence_enricher functions are imported by-name inside the
    RFP builder method (`from app.services.evidence_enricher import …`), so
    we patch the symbols at their *original* module — once they're imported
    they're bound to that module's namespace.
    """
    from unittest.mock import AsyncMock

    # Each AsyncMock returns a fresh awaitable per call, so it survives the
    # parallel asyncio.gather inside the builder.
    mocker.patch("app.services.evidence_enricher.fetch_acra_status",
                 new=AsyncMock(return_value=_ACRA_OK))
    mocker.patch("app.services.evidence_enricher.fetch_pdpc_enforcement",
                 new=AsyncMock(return_value=_PDPC_CLEAN))
    mocker.patch("app.services.evidence_enricher.fetch_ssl_grade",
                 new=AsyncMock(return_value=_SSL_OK))
    mocker.patch("app.services.evidence_enricher.fetch_domain_reputation",
                 new=AsyncMock(return_value=_DOMAIN_CLEAN))
    mocker.patch("app.services.evidence_enricher.fetch_hosting_signals",
                 new=AsyncMock(return_value=_HOSTING_OK))
    mocker.patch("app.services.evidence_enricher.check_consistency",
                 return_value=[])

    # Website scrape — patched on the class so every builder instance gets it
    from app.services.rfp_express_builder import RFPExpressBuilder
    mocker.patch.object(
        RFPExpressBuilder, "_fetch_website_context",
        new=AsyncMock(return_value=_WEBSITE_CTX),
    )

    # Blockchain anchoring
    from app.services.blockchain import BlockchainService
    mocker.patch.object(
        BlockchainService, "anchor_evidence",
        new=AsyncMock(return_value=_FAKE_TX_HASH),
    )


def _patch_ai(mocker, response: str):
    """Patch the AI call to return a canned JSON string."""
    async def _fake_call(self, messages):
        return response
    from app.services.booppa_ai_service import BooppaAIService
    mocker.patch.object(BooppaAIService, "_call_deepseek", _fake_call)


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rfp_express_builds_pdf_uploads_and_emails(
    _rfp_mocks, mocker, s3_bucket, email_capture
):
    """End-to-end: RFP Kit Express (5 Q&A) → PDF → moto S3 → email captured."""
    _patch_ai(mocker, _AI_RESPONSE_5Q)
    from app.services.rfp_express_builder import RFPExpressBuilder

    builder = RFPExpressBuilder(
        vendor_id="rfp_express@example.test",
        vendor_email="rfp_express@example.test",
        session_id="cs_test_express_smoke",
    )
    result = await builder.generate_express_package(
        vendor_url="https://acme.test",
        company_name="Acme Pte Ltd",
        rfp_details={
            "description": "Cloud migration for SG retail chain",
            "intake": {"uen": "201912345A", "dpo_name": "Jane Tan"},
        },
        db=None,
        product_type="rfp_express",
    )

    # ── return shape ──
    assert result["success"] is True
    assert result["product"] == "rfp_kit_express"
    assert result["company_name"] == "Acme Pte Ltd"
    assert result["tx_hash"] == _FAKE_TX_HASH
    assert result["download_url"].startswith("https://")
    assert result["qa_answers_count"] == 5
    assert {qa["question"] for qa in result["qa_answers"]} == {
        "Do you have a PDPA data protection policy?",
        "Has a Data Protection Officer (DPO) been appointed?",
        "What security measures are in place to protect personal data?",
        "Have there been any data breaches in the past 24 months?",
        "How do you manage third-party vendors who handle personal data?",
    }
    assert result["answer_source"] == "ai_grounded"  # AI didn't fall back to template
    assert result["data_sources"]["acra_verified"] is True
    assert result["data_sources"]["ssl_grade"] == "A+"

    # ── PDF actually generated and contains key content ──
    from app.core.config import settings as _settings
    s3_key = result["pdf_s3_key"]
    obj = s3_bucket.get_object(Bucket=_settings.S3_BUCKET, Key=s3_key)
    pdf_bytes = obj["Body"].read()
    assert pdf_bytes.startswith(b"%PDF")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    assert "Acme Pte Ltd" in text
    assert "rfp kit express" in text.lower()  # cover page renders uppercase
    # AI answer should appear
    assert "We follow PDPA strictly" in text or "PDPA" in text
    # Blockchain tx hash surfaced
    assert _FAKE_TX_HASH[:10] in text

    # ── Email captured ──
    assert any(m["to"] == "rfp_express@example.test" for m in email_capture)
    msg = email_capture[-1]
    assert "RFP Kit Express" in msg["subject"]
    assert "Acme Pte Ltd" in msg["subject"]


@pytest.mark.asyncio
async def test_rfp_complete_builds_pdf_with_15_questions(
    _rfp_mocks, mocker, s3_bucket, email_capture
):
    """RFP Kit Complete (15 Q&A) → also produces an editable DOCX."""
    _patch_ai(mocker, _AI_RESPONSE_15Q)
    from app.services.rfp_express_builder import RFPExpressBuilder

    builder = RFPExpressBuilder(
        vendor_id="rfp_complete@example.test",
        vendor_email="rfp_complete@example.test",
        session_id="cs_test_complete_smoke",
    )
    result = await builder.generate_express_package(
        vendor_url="https://acme.test",
        company_name="Acme Pte Ltd",
        rfp_details={"description": "Enterprise CRM revamp"},
        db=None,
        product_type="rfp_complete",
    )

    assert result["product"] == "rfp_kit_complete"
    assert result["qa_answers_count"] == 15
    assert result["docx_url"] is not None  # Complete tier also emits DOCX

    # Email subject reflects Complete label
    assert any("RFP Kit Complete" in m["subject"] for m in email_capture)


@pytest.mark.asyncio
async def test_rfp_express_blocks_when_ai_fails_and_template_has_placeholders(
    _rfp_mocks, mocker, s3_bucket, email_capture
):
    """Audit hard gate: if AI raises and the canned template fallback still
    contains [FILL IN] / [Verify:] placeholders (no intake supplied them), the
    kit is BLOCKED, not delivered — a GeBIZ-bound document must be complete.
    The builder returns a non-delivering `blocked` result with the missing
    fields, and never builds/anchors/emails a placeholder-laden kit.
    """
    async def _ai_boom(self, messages):
        raise RuntimeError("AI provider down")
    from app.services.booppa_ai_service import BooppaAIService
    mocker.patch.object(BooppaAIService, "_call_deepseek", _ai_boom)

    from app.services.rfp_express_builder import RFPExpressBuilder
    builder = RFPExpressBuilder(
        vendor_id="rfp_fallback@example.test",
        vendor_email="rfp_fallback@example.test",
        session_id="cs_test_fallback",
    )
    result = await builder.generate_express_package(
        vendor_url="https://acme.test",
        company_name="Acme Pte Ltd",
        rfp_details={"description": "Test fallback"},  # no intake facts → placeholders remain
        db=None,
        product_type="rfp_express",
    )

    assert result["success"] is False
    assert result["blocked"] is True
    assert result["residual_placeholders"] > 0
    assert result["missing_fields"]  # the buyer is told exactly what to complete
    # Nothing was delivered: no download URL, no kit email.
    assert "download_url" not in result
    assert not any("RFP Kit" in m["subject"] for m in email_capture)


@pytest.mark.asyncio
async def test_rfp_allow_incomplete_delivers_despite_placeholders(
    _rfp_mocks, mocker, s3_bucket, email_capture
):
    """Admin test-checkout bypass: with `allow_incomplete=True` a thin/empty
    brief that leaves residual placeholders no longer blocks — the kit is built,
    anchored, uploaded and emailed anyway so the end-to-end test yields an RFP.
    Mirrors the block test above but flips only the flag.
    """
    async def _ai_boom(self, messages):
        raise RuntimeError("AI provider down")
    from app.services.booppa_ai_service import BooppaAIService
    mocker.patch.object(BooppaAIService, "_call_deepseek", _ai_boom)

    from app.services.rfp_express_builder import RFPExpressBuilder
    builder = RFPExpressBuilder(
        vendor_id="rfp_testco@example.test",
        vendor_email="rfp_testco@example.test",
        session_id="cs_test_allow_incomplete",
    )
    result = await builder.generate_express_package(
        vendor_url="https://acme.test",
        company_name="Acme Pte Ltd",
        rfp_details={"description": "Test fallback"},  # no intake facts → placeholders remain
        db=None,
        product_type="rfp_express",
        allow_incomplete=True,
    )

    # Delivered, not blocked.
    assert result.get("blocked") is not True
    assert result["success"] is True
    assert result["download_url"].startswith("https://")
    assert result["tx_hash"] == _FAKE_TX_HASH
    # The kit email was sent.
    assert any("RFP Kit" in m["subject"] for m in email_capture)
