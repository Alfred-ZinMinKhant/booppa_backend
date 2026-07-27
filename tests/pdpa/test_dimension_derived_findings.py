"""Shared dimension→finding synthesis (incident 22fb2871, Cover Sheet leg).

`resolve_pdpa_findings` returns only what the AI persisted, and
`_detect_violations` never reads `trackers` / `policy_clauses` / `hosting` /
`pdpc_enforcement`. So a scan can score several dimensions Non-Compliant while
persisting ZERO findings.

The Quick Scan PDF was fixed to derive findings from its own score table. Every
other consumer stayed blind — most damagingly the Compliance Cover Sheet, which
ships in the same bundle and rendered "No PDPA findings were attached", a
"Minimal" risk level (all severity counts zero) and boilerplate recommendations
for a scan the PDF was simultaneously reporting as failing.

`resolve_pdpa_findings_with_dimensions` is the shared fix. These tests pin both
directions: real gaps must surface, and clean scans must stay clean.
"""
import pytest

from app.services.pdpa_findings import (
    DIMENSION_FINDING_SPEC,
    resolve_pdpa_findings,
    resolve_pdpa_findings_with_dimensions,
)

# Netpoleons shape: 6 pre-consent trackers, no banner. Cookie Consent 8/100 and
# Third-Party Tracker Inventory 30/100 — with no AI findings persisted at all.
_FAILING_NO_FINDINGS = {
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "LinkedIn Insight Tag"]},
    "consent_mechanism": {"has_cookie_banner": False},
    "nric": {"status": "Compliant", "score": 100},
}


def test_baseline_resolver_really_is_empty_here():
    """Guards the premise: without synthesis this scan yields zero findings, which
    is what every downstream renderer turned into 'you are clean'."""
    assert resolve_pdpa_findings(_FAILING_NO_FINDINGS) == []


def test_failing_dimensions_become_findings():
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    titles = {f["title"] for f in out}

    assert "Cookie Consent Mechanism" in titles
    assert "Third-Party Tracker Inventory" in titles
    # Non-Compliant must carry a severity the Cover Sheet's risk-level ladder
    # and severity_counts can actually see — "Minimal" was the bug.
    assert all(f["severity"] == "HIGH" for f in out)
    assert all(f["derived_from_dimension"] is True for f in out)


def test_derived_findings_carry_what_renderers_read():
    """The Cover Sheet renders severity/title/description/recommendation and ranks
    by severity. A synthesized finding missing these renders as a blank card."""
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    f = next(f for f in out if f["title"] == "Cookie Consent Mechanism")

    assert f["type"] == "cookie_consent_violation"
    assert f["description"]
    assert f["requirements"]
    assert f["deadline"] == "7 days"


def test_compliant_dimensions_never_synthesize():
    """A passing dimension must never produce a finding — the inverse failure is
    telling a clean customer they have a gap."""
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    assert "NRIC Exposure" not in {f["title"] for f in out}


def test_clean_scan_stays_clean():
    out = resolve_pdpa_findings_with_dimensions({
        "nric": {"status": "Compliant", "score": 100},
        "consent_mechanism": {"has_cookie_banner": True},
    })
    assert out == []


def test_ai_findings_win_over_synthesis():
    """When the AI already wrote a finding for a dimension, we must not duplicate
    it — the buyer would see the same gap twice with different wording."""
    ad = dict(_FAILING_NO_FINDINGS)
    ad["booppa_report"] = {"detailed_findings": [{
        "type": "cookie_consent_violation",
        "title": "No consent banner present",
        "severity": "CRITICAL",
    }]}

    out = resolve_pdpa_findings_with_dimensions(ad)
    cookie_ish = [f for f in out if "cookie" in (f.get("type") or "")]

    assert len(cookie_ish) == 1
    assert cookie_ish[0]["severity"] == "CRITICAL"  # the AI's, not the synthesized HIGH
    # ...but the uncovered tracker dimension must still be filled.
    assert "Third-Party Tracker Inventory" in {f["title"] for f in out}


def test_nested_ai_findings_are_preserved():
    """Synthesis must sit on top of the nesting fix, not replace it."""
    ad = dict(_FAILING_NO_FINDINGS)
    ad["booppa_report"] = {"detailed_findings": [
        {"type": "unrelated_violation", "title": "Something else", "severity": "LOW"}
    ]}
    titles = {f["title"] for f in resolve_pdpa_findings_with_dimensions(ad)}
    assert "Something else" in titles


@pytest.mark.parametrize("dimension", [
    "Cookie Consent Mechanism",
    "Third-Party Tracker Inventory",
    "Privacy Policy (PDPA §11/13)",
    "Retention Limitation (§25)",
    "Data Breach Notification (§26B-D)",
    "Cross-Border Transfer (§26)",
    "NRIC Exposure",
])
def test_every_snapshot_dimension_has_a_spec(dimension):
    """`compute_dimension_snapshots` can emit each of these. Any one missing from
    the spec is silently un-synthesizable — a failing dimension that produces no
    finding, which is the original bug."""
    assert dimension in DIMENSION_FINDING_SPEC


def test_pdf_service_shares_the_same_spec():
    """A second copy of the table next to one renderer is how the Quick Scan PDF
    and the Cover Sheet came to describe the same scan differently."""
    from app.services.pdf_service import PDFService

    assert PDFService._DIMENSION_FINDING_SPEC is DIMENSION_FINDING_SPEC
