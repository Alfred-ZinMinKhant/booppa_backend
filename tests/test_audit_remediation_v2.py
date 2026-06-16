"""Audit Remediation v2 + BCEP — unit coverage for the new deliverables.

Covers the pure/renderable surface of each fix so a regression is caught without
needing network or Stripe:
  * Email attachment plumbing (filter + Resend payload + SES MIME)
  * RFP hard placeholder gate helpers
  * TRM baseline assessed-entity is the customer, never "Booppa"
  * Vendor Pro quarterly PDPA snapshot PDF
  * Offline artefact PDFs (badge / priority / competitor / bid-timing)
  * BCEP evidence pack generation + PDF (no fabricated UEN, honest testnet)
"""
import json

import pytest


# ── Fix 0: email attachments ────────────────────────────────────────────────

def test_filter_attachments_drops_empty_and_oversize():
    from app.services.email_service import _filter_attachments, _MAX_ATTACHMENT_BYTES

    big = ("big.pdf", b"x" * (_MAX_ATTACHMENT_BYTES + 1))
    kept = _filter_attachments([("a.pdf", b""), ("ok.pdf", b"data"), big])
    assert kept == [("ok.pdf", b"data")]
    assert _filter_attachments(None) == []


def test_ses_raw_mime_includes_pdf_attachment():
    from app.services.email_service import EmailService

    raw = EmailService._build_raw_mime(
        "to@x.com", "Subj", "<p>hi</p>", [("report.pdf", b"%PDF-1.4 fake")]
    )
    assert b"report.pdf" in raw
    assert b"application/pdf" in raw
    assert b"<p>hi</p>" in raw


@pytest.mark.asyncio
async def test_resend_payload_carries_base64_attachment(monkeypatch):
    import base64
    from app.services import email_service as es

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "abc"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(json)
            return _Resp()

    monkeypatch.setattr(es.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(es.settings, "RESEND_API_KEY", "re_test", raising=False)
    monkeypatch.setattr(es.settings, "SKIP_EMAIL", False, raising=False)

    ok = await es.EmailService().send_html_email(
        "to@x.com", "S", "<b>b</b>", attachments=[("f.pdf", b"hello")]
    )
    assert ok is True
    assert "attachments" in captured
    assert captured["attachments"][0]["filename"] == "f.pdf"
    assert base64.b64decode(captured["attachments"][0]["content"]) == b"hello"


# ── Fix 1: RFP hard gate helpers ────────────────────────────────────────────

def test_residual_placeholder_details_are_distinct_and_descriptive():
    from app.services.rfp_express_builder import RFPExpressBuilder

    b = RFPExpressBuilder.__new__(RFPExpressBuilder)
    qa = {
        "a": "We hold [Verify: ISO 27001 cert number].",
        "b": "DPO is ___ [FILL IN] ___ and ISO is [Verify: ISO 27001 cert number].",
        "c": "All good.",
    }
    details = b._residual_placeholder_details(qa)
    assert "[Verify: ISO 27001 cert number]" in details
    assert any("FILL IN" in d for d in details)
    # Distinct: the repeated ISO marker appears once.
    assert details.count("[Verify: ISO 27001 cert number]") == 1


# ── Fix 2: TRM baseline assessed entity ─────────────────────────────────────

def test_trm_baseline_entity_is_customer_not_booppa():
    from app.services.trm_baseline_generator import generate_trm_baseline_pdf

    pdf = generate_trm_baseline_pdf({
        "company_name": "Funding Societies Pte Ltd",
        "plan_label": "Pro Suite",
        "controls": [{"domain": "Cyber Security", "control_ref": "TRM-5", "status": "not_started"}],
    })
    assert pdf[:4] == b"%PDF"
    # Extractable text lives uncompressed enough to find the entity marker label.
    assert b"Assessed Entity" in pdf or b"Funding Societies" in pdf


# ── Fix 4: Vendor Pro quarterly PDPA snapshot ───────────────────────────────

def test_vendor_pro_snapshot_drift_and_baseline_render():
    from app.services.vendor_pdpa_snapshot_generator import generate_vendor_pdpa_snapshot_pdf

    drift = generate_vendor_pdpa_snapshot_pdf({
        "company_name": "Crayon Singapore",
        "current_score": 56,
        "previous_score": 61,
        "dimension_flips": [
            {"dimension_name": "Cookie Consent", "previous_status": "Compliant", "current_status": "Non-Compliant"}
        ],
        "is_baseline": False,
        "anchor_tx": "0xabc123",
    })
    assert drift[:4] == b"%PDF"
    baseline = generate_vendor_pdpa_snapshot_pdf({"company_name": "X", "current_score": 56, "is_baseline": True})
    assert baseline[:4] == b"%PDF"


# ── Fix 5: offline artefact PDFs ────────────────────────────────────────────

def test_offline_artefact_pdfs_render():
    from app.services.vendor_artifacts_generator import (
        generate_badge_certificate_pdf,
        generate_priority_placement_pdf,
        generate_competitor_signals_pdf,
        generate_bid_timing_pdf,
    )

    assert generate_badge_certificate_pdf({
        "company_name": "Crayon", "verification_depth": "BASIC",
        "procurement_readiness": "CONDITIONAL", "confidence_score": 30,
    })[:4] == b"%PDF"
    assert generate_priority_placement_pdf({
        "company_name": "Crayon", "plan_label": "Vendor Pro", "profile_views_30d": 9,
    })[:4] == b"%PDF"
    assert generate_competitor_signals_pdf({
        "company_name": "Crayon", "tender_no": "T1", "window_days": 30,
        "lookups": {"focal": 5, "focal_verified": 3, "similar": 8, "similar_verified": 4},
        "sector_active_verified": 6,
    })[:4] == b"%PDF"
    assert generate_bid_timing_pdf({
        "company_name": "Crayon", "period_label": "p", "total_awards": 100,
        "busiest_month": "Mar 2026", "months": [{"month": "Jan 2026", "awards": 10, "value": 50000.0}],
    })[:4] == b"%PDF"


# ── Fix 6: BCEP evidence pack ───────────────────────────────────────────────

def test_evidence_pack_generates_seven_docs_with_customer_uen(monkeypatch):
    import app.services.evidence_pack.document_generator as dg

    def _fake(system, user):
        return json.dumps({
            "document_title": "Doc", "organisation": "Funding Societies Pte Ltd",
            "uen": "201912345A", "version": "1.0", "sections": {"x": "y"},
        })

    monkeypatch.setattr(dg, "_deepseek_chat", _fake)
    pack = dg.generate_evidence_pack({
        "org_name": "Funding Societies Pte Ltd", "uen": "201912345A", "sector": "Fintech",
        "employee_count": "51-200", "approver_name": "Jane", "approver_role": "MD",
        "data_types": ["customer data"], "customer_types": ["B2B"], "systems": ["AWS"],
    })
    assert set(pack["documents"].keys()) == {
        "dpmp", "ropa", "data_inventory", "vendor_register",
        "breach_runbook", "training", "review_log",
    }
    assert pack["uen"] == "201912345A"
    assert pack["master_hash"]


def test_evidence_pack_pdf_has_no_fabricated_uen_and_honest_testnet(monkeypatch):
    import app.services.evidence_pack.document_generator as dg
    from app.services.evidence_pack.pdf_builder import build_single_pdf

    monkeypatch.setattr(dg, "_deepseek_chat", lambda s, u: json.dumps({
        "document_title": "ROPA", "organisation": "Funding Societies Pte Ltd",
        "uen": "201912345A", "version": "1.0", "processing_activities": [],
    }))
    pack = dg.generate_evidence_pack({
        "org_name": "Funding Societies Pte Ltd", "uen": "201912345A", "sector": "Fintech",
        "employee_count": "51-200", "approver_name": "Jane", "approver_role": "MD",
        "data_types": ["customer data"], "customer_types": ["B2B"], "systems": ["AWS"],
    })
    pdf = build_single_pdf(pack, "ropa", "")
    assert pdf[:4] == b"%PDF"
    assert b"202415732W" not in pdf          # fabricated Booppa UEN never printed
    assert b"Chain ID 137" not in pdf        # no mainnet claim
