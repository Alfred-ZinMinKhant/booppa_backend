"""Compliance Evidence Pack — scan grounding invariants.

`_evidence_context` builds the OBSERVED EVIDENCE block appended to every
document prompt. It is the only channel through which the scan's verdict
reaches the LLM. Two defects made that channel one-sided:

  1. findings were read off the TOP LEVEL of `assessment_data`, but the AI
     report is persisted nested under `booppa_report` — so `pf` was [] on
     every pack (same defect class as PDPA report 22fb2871);
  2. only the *Compliant* dimensions were surfaced, never the failing ones.

Together, a scan scoring Cookie Consent 8/100 handed the LLM a block that
said nothing was wrong and listed what passed — and the pack asserted
compliance the evidence contradicted.
"""
import pytest

from app.services.evidence_pack.document_generator import _evidence_context


_FINDING = {
    "type": "cookie_consent_violation",
    "title": "Trackers fire before consent",
    "severity": "HIGH",
}

# assessment_data for a site with 6 pre-consent trackers and no banner:
# Cookie Consent 8/100, Third-Party Tracker Inventory 30/100.
_FAILING_SCAN = {
    "consent_mechanism": {"has_cookie_banner": False},
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "LinkedIn Insight Tag"]},
    "nric": {"status": "Compliant", "score": 100},
}


@pytest.mark.parametrize("label,assessment", [
    ("modern nested", {"booppa_report": {"detailed_findings": [_FINDING]}}),
    ("nested risk_assessment", {"booppa_report": {"risk_assessment": {"findings": [_FINDING]}}}),
    ("top-level legacy", {"detailed_findings": [_FINDING]}),
    ("legacy violations", {"violations": [_FINDING]}),
])
def test_nested_findings_reach_the_prompt(label, assessment):
    """The top-level-only read resolved [] for every one of these shapes."""
    out = _evidence_context({"pdpa_report": {**assessment, **_FAILING_SCAN}})
    assert "Trackers fire before consent" in out, f"{label}: finding never reached the LLM"


def test_failing_dimensions_are_surfaced():
    """The score table's failing rows must appear, with their scores, and be
    labelled as confirmed gaps — not merely omitted from the compliant list."""
    out = _evidence_context({"pdpa_report": dict(_FAILING_SCAN)})

    assert "Cookie Consent Mechanism — 8/100 (Non-Compliant)" in out
    assert "Third-Party Tracker Inventory — 30/100 (Non-Compliant)" in out
    assert "BELOW Compliant" in out
    assert "do NOT assert" in out


def test_compliant_dimensions_are_still_surfaced():
    """The inverse guard: the anti-hallucination signal must not be lost. A
    passing dimension may never be listed as a gap."""
    out = _evidence_context({"pdpa_report": dict(_FAILING_SCAN)})

    assert "NRIC Exposure (100/100)" in out
    assert "do NOT list these as gaps" in out
    # …and it must not also appear in the failing block.
    failing_block = out.split("found COMPLIANT")[0]
    assert "NRIC Exposure —" not in failing_block


def test_clean_scan_surfaces_no_gap_block():
    """Nothing may be reported as failing when every assessed dimension passes
    — the fix must not push clean customers into a remediation narrative."""
    out = _evidence_context({"pdpa_report": {
        "nric": {"status": "Compliant", "score": 100},
        "consent_mechanism": {"has_cookie_banner": True},
    }})

    assert "BELOW Compliant" not in out
    assert "found COMPLIANT" in out


def test_no_evidence_still_returns_empty():
    """Intake-only generation (no scan at all) must be unchanged."""
    assert _evidence_context(None) == ""
    assert _evidence_context({}) == ""
