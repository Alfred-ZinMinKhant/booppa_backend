"""Regression tests for the shared PDPA findings resolver.

Locks the invariant that produced the "0 vs 2 open findings" contradiction:
the Quick Scan nests findings under booppa_report.detailed_findings, and every
consumer (Cover Sheet, Monitor Report, Vendor snapshot) must resolve the same
list from that same row.
"""

from app.services.pdpa_findings import resolve_pdpa_findings, resolve_pdpa_score


def test_resolves_modern_nested_location():
    ad = {
        "booppa_report": {
            "detailed_findings": [
                {"severity": "HIGH", "title": "NRIC exposed"},
                {"severity": "MEDIUM", "title": "No cookie banner"},
            ]
        }
    }
    findings = resolve_pdpa_findings(ad)
    assert len(findings) == 2


def test_monitor_and_cover_sheet_agree_on_count():
    # The exact shape process_report_task persists — findings ONLY under
    # booppa_report. The old Monitor read of top-level keys saw [] here.
    ad = {"booppa_report": {"detailed_findings": [{"severity": "HIGH"}]}}
    assert len(resolve_pdpa_findings(ad)) == 1
    # top-level access (the old buggy path) would have been empty:
    assert not (ad.get("detailed_findings") or ad.get("findings"))


def test_falls_back_to_legacy_top_level_keys():
    assert len(resolve_pdpa_findings({"detailed_findings": [{"x": 1}]})) == 1
    assert len(resolve_pdpa_findings({"findings": [{"x": 1}, {"x": 2}]})) == 2
    assert len(resolve_pdpa_findings({"risk_assessment": {"findings": [{"x": 1}]}})) == 1
    assert len(resolve_pdpa_findings({"violations": [{"x": 1}]})) == 1


def test_coerces_dict_of_findings_to_list():
    ad = {"booppa_report": {"detailed_findings": {"a": {"x": 1}, "b": {"x": 2}}}}
    assert len(resolve_pdpa_findings(ad)) == 2


def test_empty_and_malformed_inputs_return_empty_list():
    assert resolve_pdpa_findings(None) == []
    assert resolve_pdpa_findings({}) == []
    assert resolve_pdpa_findings("not a dict") == []
    assert resolve_pdpa_findings({"booppa_report": {"detailed_findings": None}}) == []


def test_nested_location_takes_precedence_over_legacy():
    # Modern nested findings win over a stale top-level list.
    ad = {
        "booppa_report": {"detailed_findings": [{"a": 1}, {"b": 2}]},
        "findings": [{"stale": True}],
    }
    findings = resolve_pdpa_findings(ad)
    assert len(findings) == 2
    assert findings[0] == {"a": 1}


# ── resolve_pdpa_score ──────────────────────────────────────────────────────
# Locks the "66 vs not available" contradiction: Cover Sheet and RFP Supplier
# Declaration must resolve the SAME score from the same assessment_data.


def test_score_uses_persisted_compliance_score_verbatim():
    # Canonical compliance_score is authoritative — never recomputed.
    assert resolve_pdpa_score({"compliance_score": 66}) == 66
    assert resolve_pdpa_score({"compliance_score": 65.6}) == 66


def test_score_derives_from_risk_when_no_compliance_score():
    # 0 = clean risk, 100 = high risk → compliance = 100 - risk.
    assert resolve_pdpa_score({"overall_risk_score": 34}) == 66
    assert resolve_pdpa_score({"score": 34}) == 66
    assert resolve_pdpa_score({"risk_score": 10}) == 90
    assert resolve_pdpa_score(
        {"booppa_report": {"risk_assessment": {"score": 20}}}
    ) == 80


def test_score_compliance_score_wins_over_risk():
    ad = {"compliance_score": 66, "overall_risk_score": 90}
    assert resolve_pdpa_score(ad) == 66


def test_score_none_when_unresolvable():
    assert resolve_pdpa_score(None) is None
    assert resolve_pdpa_score({}) is None
    assert resolve_pdpa_score("nope") is None
    assert resolve_pdpa_score({"compliance_score": "bad", "risk_score": "bad"}) is None


def test_score_clamped_to_0_100():
    assert resolve_pdpa_score({"risk_score": 150}) == 0
    assert resolve_pdpa_score({"risk_score": -50}) == 100
