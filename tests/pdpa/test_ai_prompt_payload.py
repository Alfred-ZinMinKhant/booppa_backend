"""The DeepSeek call: what we send, what we allow back, and failing loudly.

Three defects this pins:

1. The prompt sent `report.assessment_data` WHOLE. On a rescan that already
   contains `booppa_report` — the previous run's complete structured report,
   every finding description included. We paid to feed last month's report back
   into this month's, growing with each rescan, and handed the model stale
   conclusions to anchor on.
2. `max_tokens=1400` truncated multi-violation reports mid-JSON. The parse then
   failed and the whole response was discarded for the static templates — we
   paid for all 1400 tokens and used none of them.
3. Both failures were SILENT. The only trace was `report_metadata.ai_model`
   flipping to "Booppa Template Engine".
"""
import asyncio
import json
import logging

import pytest

from app.services.ai_provider import DeepSeekProvider
from app.services.booppa_ai_service import SCAN_EVIDENCE_KEYS, BooppaAIService


def _scan_data_with_previous_report():
    """A rescan: raw evidence plus the bulky derived output of the prior run."""
    return {
        "url": "https://netpoleons.com",
        "company_name": "Netpoleons",
        "site_accessible": True,
        "trackers": {"inventory": ["Google Analytics", "Meta Pixel"]},
        "consent_mechanism": {"has_cookie_banner": False},
        # Derived output that must never be sent back to the model.
        "booppa_report": {
            "detailed_findings": [
                {"type": "cookie_violation", "description": "x" * 4000}
            ],
            "executive_summary": "y" * 4000,
        },
        "intake_data": {"notes": "z" * 2000},
    }


class _Capture:
    """Stands in for the provider; records the request instead of calling out."""

    def __init__(self):
        self.messages = None
        self.json_mode = None

    async def call_chat(self, messages, json_mode=False):
        self.messages = messages
        self.json_mode = json_mode
        return None  # force the fallback path; we only care about the request


def _capture_payload(scan_data):
    svc = BooppaAIService()
    svc.deepseek_api_key = "test-key"
    cap = _Capture()
    svc._deepseek_provider = cap
    asyncio.run(
        svc._generate_deepseek_report_payload(
            scan_data, svc._detect_violations(scan_data), {"level": "HIGH"}
        )
    )
    body = cap.messages[1]["content"]
    return cap, json.loads(body[body.index("{", body.index("INPUT:")):])


def test_previous_report_is_not_sent_back_to_the_model():
    """The single biggest cost item, and it compounded on every rescan."""
    _cap, payload = _capture_payload(_scan_data_with_previous_report())

    assert "booppa_report" not in payload["scan_data"]
    assert "intake_data" not in payload["scan_data"]


def test_prompt_shrinks_substantially_on_a_rescan():
    sd = _scan_data_with_previous_report()
    _cap, payload = _capture_payload(sd)

    before = len(json.dumps(sd))
    after = len(json.dumps(payload["scan_data"]))
    assert after < before / 2, f"{before} -> {after}"


def test_real_evidence_still_reaches_the_model():
    """The trim must not starve the model of what it needs to be accurate —
    that would trade one false-document failure for another."""
    _cap, payload = _capture_payload(_scan_data_with_previous_report())
    sent = payload["scan_data"]

    assert sent["trackers"]["inventory"] == ["Google Analytics", "Meta Pixel"]
    assert sent["consent_mechanism"]["has_cookie_banner"] is False
    assert sent["url"] == "https://netpoleons.com"
    assert sent["company_name"] == "Netpoleons"


def test_violations_are_still_sent_in_full():
    """Trimming scan_data must not touch the violations block — that IS the
    material the model is asked to describe."""
    _cap, payload = _capture_payload(_scan_data_with_previous_report())
    assert payload["violations"]
    for v in payload["violations"]:
        assert v["details"] and v["evidence"] and v["legislation"]


def test_json_mode_is_requested():
    """Without it, only the prompt text asks for JSON — one line of preamble
    makes the response unparseable and silently drops us to templates."""
    cap, _payload = _capture_payload(_scan_data_with_previous_report())
    assert cap.json_mode is True


@pytest.mark.parametrize("key", ["trackers", "policy_clauses", "hosting", "pdpc_enforcement"])
def test_tier_keys_are_in_the_allowlist(key):
    """These are the keys the score table grades. A key missing here is evidence
    the model never sees — the original incident, one layer over."""
    assert key in SCAN_EVIDENCE_KEYS


def test_scan_evidence_keys_carry_no_derived_output():
    """An allowlist is only a cost control if derived keys stay out of it."""
    for banned in ("booppa_report", "detailed_findings", "intake_data",
                   "compliance_score", "risk_assessment"):
        assert banned not in SCAN_EVIDENCE_KEYS


def test_pdf_path_uses_the_same_allowlist():
    """`tasks.py` had a parallel copy. Two lists means the model and the score
    table can silently come to reason over different evidence."""
    import inspect
    from app.workers import tasks

    src = inspect.getsource(tasks)
    assert "SCAN_EVIDENCE_KEYS" in src
    assert '"policy_clauses", "pdpc_enforcement", "hosting",' not in src


# ── Provider-level ──────────────────────────────────────────────────────────


def test_token_ceiling_fits_a_multi_violation_report():
    """1400 truncated once the detector started reporting every failing
    dimension (3 violations -> 6)."""
    import app.services.ai_provider as mod

    import inspect

    src = inspect.getsource(mod)
    assert '"max_tokens": 4000' in src
    assert '"max_tokens": 1400' not in src


def test_truncation_is_logged(caplog):
    """A truncated response returns HTTP 200 with partial content — finish_reason
    is the only signal, and nothing was reading it."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": '{"partial"'}}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    import app.core.http_client as hc

    original = hc.get_deepseek_client
    hc.get_deepseek_client = lambda: _Client()
    try:
        with caplog.at_level(logging.WARNING):
            asyncio.run(DeepSeekProvider("k").call_chat([{"role": "user", "content": "x"}]))
    finally:
        hc.get_deepseek_client = original

    assert any("truncated" in r.message.lower() for r in caplog.records)


def test_unparseable_output_is_logged(caplog):
    """The fallback to static templates was completely silent."""
    svc = BooppaAIService()
    svc.deepseek_api_key = "test-key"

    class _Garbage:
        async def call_chat(self, messages, json_mode=False):
            return "I'm sorry, I cannot help with that."

    svc._deepseek_provider = _Garbage()
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(
            svc._generate_deepseek_report_payload({"url": "https://x.sg"}, [], {})
        )

    assert out == {"used_deepseek": False}
    assert any("unparseable" in r.message.lower() for r in caplog.records)
