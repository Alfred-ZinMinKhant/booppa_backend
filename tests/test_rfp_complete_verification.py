"""Phase 3 invariants for the RFP Complete kit.

Two forensic findings drove these:

  1. The Complete tier (SGD 599) is distinguished from Express (SGD 249) by the
     editable DOCX evidence pack. A build/upload failure previously shipped a
     PDF-only kit silently — the buyer never got what they paid for.
  2. Every answer fell back to the amber "AI-drafted" badge. Root cause was a
     no-intake, unreachable-URL test run — but the verification map must earn
     the green VERIFIED badge for eligible answers when a realistic intake is
     supplied.

The VERIFIED badge is driven by ``verification_map[k].source != "ai_drafted"``.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

import app.services.fulfillment.single_products as sp
from app.services.fulfillment.single_products import _RfpDeliverableIncomplete, _fulfill_rfp_package
from app.services.rfp_express_builder import RFPExpressBuilder


def _run_fulfill(monkeypatch, *, product_type, result, allow_incomplete=False):
    """Drive _fulfill_rfp_package with a stubbed builder result. Returns the list
    of fired support alerts; propagates whatever the function raises."""
    monkeypatch.setattr(sp, "SessionLocal", lambda: MagicMock())

    async def _fake_generate(self, **kwargs):
        return result

    monkeypatch.setattr(
        RFPExpressBuilder, "generate_express_package", _fake_generate
    )
    alerts: list = []

    async def _fake_alert(**kwargs):
        alerts.append(kwargs)
        return True

    monkeypatch.setattr(sp, "_alert_payment_fulfillment_issue", _fake_alert)
    monkeypatch.setattr(sp, "_maybe_fire_cover_sheet", lambda *a, **k: None)
    coro = _fulfill_rfp_package(
        product_type=product_type,
        vendor_id="v1",
        vendor_email="",  # empty → skip the Report-persist DB branch
        vendor_url="https://acme.sg",
        company_name="Acme Pte Ltd",
        session_id="cs_test_123",
        allow_incomplete=allow_incomplete,
    )
    try:
        asyncio.run(coro)
    finally:
        _run_fulfill.last_alerts = alerts
    return alerts


def test_rfp_complete_without_docx_raises_for_retry(monkeypatch):
    """A Complete kit that built a PDF but no DOCX must raise (→ Celery retry)
    and alert support — never silently ship PDF-only."""
    with pytest.raises(_RfpDeliverableIncomplete):
        _run_fulfill(
            monkeypatch,
            product_type="rfp_complete",
            result={"download_url": "https://s3/pdf", "docx_url": None},
        )
    # alert fired before the raise
    alerts = _run_fulfill.last_alerts
    assert alerts and alerts[0]["product_type"] == "rfp_complete"


def test_rfp_express_without_docx_delivers(monkeypatch):
    """Express has no DOCX deliverable — a missing docx_url must NOT block it."""
    # No raise expected.
    _run_fulfill(
        monkeypatch,
        product_type="rfp_express",
        result={"download_url": "https://s3/pdf", "docx_url": None},
    )


def test_rfp_complete_test_path_exempt_from_docx_gate(monkeypatch):
    """allow_incomplete (admin-sim / e2e harness) must still deliver so the
    test can retrieve a kit without a live S3 bucket."""
    _run_fulfill(
        monkeypatch,
        product_type="rfp_complete",
        result={"download_url": "https://s3/pdf", "docx_url": None},
        allow_incomplete=True,
    )


def _builder() -> RFPExpressBuilder:
    return RFPExpressBuilder(vendor_id="vendor@test.io", vendor_email="vendor@test.io")


def test_realistic_intake_earns_verified_answers():
    """A realistic intake must produce at least one answer with a non-ai_drafted
    source — i.e. at least one green VERIFIED badge."""
    intake = {
        "uen": "201812345A",
        "dpo_appointed": "yes",
        "dpo_name": "Jane Tan",
        "dpo_email": "dpo@acme.sg",
        "iso_status": "certified",
        "iso_cert_number": "ISMS-2021-0042",
        "iso_cert_expiry": "2026-05-01",
        "bcp_last_tested": "2025-11-15",
        "training_frequency": "quarterly",
        "key_processors": "AWS, Stripe",
        "breach_history": "none",
    }
    vmap = _builder()._compute_verification(
        intake=intake,
        vendor_ctx={"uen": "201812345A"},
        website_signals={},
    )
    verified = {k: v for k, v in vmap.items() if v["source"] != "ai_drafted"}
    assert verified, "realistic intake produced zero VERIFIED answers"
    # Spot-check the fields the buyer explicitly declared.
    for key in ("dpo_appointed", "iso_certifications", "business_continuity", "staff_training"):
        assert key in vmap, f"{key} should be backed by intake evidence"
        assert vmap[key]["source"] != "ai_drafted"


def test_external_signals_earn_verified_without_intake():
    """Even with no intake, external/website signals (SSL grade, published
    privacy policy, PDPA mention) must earn VERIFIED — the paid path gathers
    these regardless of intake."""
    vmap = _builder()._compute_verification(
        intake={},
        vendor_ctx={"privacy_policy_url": "https://acme.sg/privacy"},
        website_signals={"pdpa_mentioned": True, "iso_27001_mentioned": True},
        ssl_result={"grade": "A+"},
    )
    assert vmap.get("security_measures", {}).get("source") == "ssl"
    assert vmap.get("data_policy", {}).get("source") == "website"


def test_no_evidence_yields_no_verified_entries():
    """The forensic baseline: no intake, no external signals → nothing earns a
    VERIFIED source, so every answer falls back to ai_drafted downstream."""
    vmap = _builder()._compute_verification(
        intake={}, vendor_ctx={}, website_signals={},
    )
    assert vmap == {}
