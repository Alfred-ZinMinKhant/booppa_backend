"""The detector must read every scored dimension (incident 22fb2871, source leg).

`_detect_violations` is the gate for the WHOLE report: the DeepSeek prompt can
only describe violations already in the list (it cannot add one), and the risk
score, recommendations, references and next steps are all derived from it. So a
dimension with no rule in the detector is invisible to the entire product, no
matter how good the scan evidence was.

These tests pin the netpoleons shape: real tracker evidence, no consent banner,
zero findings previously possible.
"""
import asyncio

import pytest

from app.services.booppa_ai_service import (
    BooppaAIService,
    calculate_risk_score,
    get_penalty_for_violation,
)

# What an unmapped slug degrades to — asserting against the literal keeps these
# tests honest if the fallback wording ever changes.
_GENERIC_LEGISLATION = ["PDPA 2012 (general provisions)"]
from app.services.pdpa_findings import (
    DIMENSION_FINDING_SPEC,
    dimension_violations,
    resolve_pdpa_findings_with_dimensions,
)

# 6 pre-consent trackers, no banner, policy missing retention, hosting overseas.
# Every one of these keys was previously unread by `_detect_violations`.
_NETPOLEONS = {
    "url": "https://netpoleons.com",
    "site_accessible": True,
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "LinkedIn Insight Tag"]},
    "consent_mechanism": {"has_cookie_banner": False},
    "policy_clauses": {
        "status": "Non-Compliant",
        "score": 40,
        "items": [{"clause": "retention", "present": False}],
    },
    "hosting": {"checked": True, "inferred_provider": "AWS", "inferred_region": "US"},
    "nric": {"status": "Compliant", "score": 100},
}


def _svc():
    return BooppaAIService()


def _types(violations):
    return {v["type"] for v in violations}


def test_tier_keys_now_produce_violations():
    """The four dimensions that previously had no possible violation."""
    got = _types(_svc()._detect_violations(_NETPOLEONS))

    assert "tracker_inventory_violation" in got
    assert "retention_limitation_violation" in got
    assert "cross_border_transfer_violation" in got
    # The Privacy Policy dimension (policy_clauses Non-Compliant) is covered by
    # the legacy DPO rule here, which fires because `dpo_compliance` is absent —
    # see test_dpo_rule_suppresses_duplicate_privacy_policy_violation.
    assert "organizational_violation" in got


def test_privacy_policy_dimension_fires_when_dpo_rule_does_not():
    """With a disclosed DPO the legacy rule stays silent, so the failing
    policy_clauses evidence must surface on its own."""
    ad = dict(_NETPOLEONS)
    ad["dpo_compliance"] = {"has_dpo": True}
    got = _types(_svc()._detect_violations(ad))

    assert "organizational_violation" not in got
    assert "privacy_policy_violation" in got


def test_cookie_gap_is_reported_exactly_once():
    """The legacy keyword rule and the dimension rule both cover cookie consent.
    Emitting both would show the buyer the same gap twice with different wording.
    """
    got = [v for v in _svc()._detect_violations(_NETPOLEONS) if "cookie" in v["type"]]
    assert len(got) == 1
    # The legacy rule wins: it has the richer, evidence-specific narrative.
    assert got[0]["type"] == "cookie_violation"


def test_dpo_rule_suppresses_duplicate_privacy_policy_violation():
    """`organizational_violation` (missing DPO) covers the Privacy Policy
    dimension, so the dimension must not add a second one."""
    ad = dict(_NETPOLEONS)
    ad["dpo_compliance"] = {"has_dpo": False}
    got = _svc()._detect_violations(ad)

    assert "organizational_violation" in _types(got)
    assert "privacy_policy_violation" not in _types(got)


def test_derived_violations_carry_the_detector_shape():
    """`_generate_violation_detail` reads these keys; a missing one renders blank."""
    v = next(
        v for v in _svc()._detect_violations(_NETPOLEONS)
        if v["type"] == "tracker_inventory_violation"
    )
    for key in ("type", "severity", "details", "location", "evidence"):
        assert v.get(key), f"missing {key}"
    assert v["derived_from_dimension"] is True
    assert "netpoleons.com" in v["location"]


def test_derived_violations_have_real_citations():
    """Falling through to 'PDPA General Provisions' is not an actionable citation."""
    svc = _svc()
    for v in svc._detect_violations(_NETPOLEONS):
        legislation = svc._get_violation_legislation(v["type"])
        assert legislation != ["PDPA General Provisions"], v["type"]


def test_risk_score_now_reflects_the_failing_dimensions():
    """The whole point: violations feed `calculate_risk_score`. Previously this
    scan produced a near-clean risk score."""
    got = _svc()._detect_violations(_NETPOLEONS)
    assert calculate_risk_score(got) >= 60


def test_clean_scan_produces_no_derived_violations():
    """Inverse direction — the fix must not invent gaps for compliant sites."""
    clean = {
        "url": "https://example.sg",
        "site_accessible": True,
        "nric": {"status": "Compliant", "score": 100},
        "consent_mechanism": {"has_cookie_banner": True, "has_active_consent": True},
        "trackers": {"inventory": []},
        "hosting": {"checked": True, "inferred_region": "Singapore"},
    }
    assert dimension_violations(clean, []) == []


def test_inaccessible_site_still_short_circuits():
    """A site we could not reach must produce NO findings — empty scan data is
    not evidence of non-compliance."""
    ad = dict(_NETPOLEONS)
    ad["site_accessible"] = False
    assert _svc()._detect_violations(ad) == []


def test_unevidenced_dimensions_are_skipped():
    """`dimension_violations` may only raise a gap the score table also shows."""
    got = dimension_violations({"url": "https://x.sg"}, [])
    assert got == []


def test_renderer_synthesis_does_not_double_up_on_the_source_fix():
    """Once the detector emits these, the render-time safety net must recognise
    them as covered — otherwise every document lists each gap twice."""
    persisted = {
        **_NETPOLEONS,
        "booppa_report": {"detailed_findings": _svc()._detect_violations(_NETPOLEONS)},
    }
    out = resolve_pdpa_findings_with_dimensions(persisted)
    titles = [f.get("title") or f.get("type") for f in out]
    assert len(titles) == len(set(titles)), titles


@pytest.mark.parametrize("slug", sorted({s[1] for s in DIMENSION_FINDING_SPEC.values()}))
def test_every_spec_slug_has_legislation(slug):
    assert _svc()._get_violation_legislation(slug) != _GENERIC_LEGISLATION


# ── Rendered detail: the finding a customer actually reads ──────────────────


def _details():
    svc = _svc()
    return asyncio.run(_render_all(svc))


async def _render_all(svc):
    return [
        await svc._generate_violation_detail(v, _NETPOLEONS)
        for v in svc._detect_violations(_NETPOLEONS)
    ]


def test_every_rendered_finding_has_actionable_remediation():
    """`VIOLATION_META` covers only the six keyword checks, so the dimension
    slugs rendered a §5 task with an EMPTY body — telling a customer they have a
    gap and giving them nothing to do about it."""
    for d in _details():
        assert d["requirements"], f"{d['type']} has no requirements"
        assert d["acceptance_criteria"], f"{d['type']} has no acceptance criteria"


def test_rendered_titles_are_human_readable():
    """Without spec meta the title fell back to 'Tracker Inventory Violation'."""
    titles = {d["type"]: d["title"] for d in _details()}
    assert titles["tracker_inventory_violation"] == "Third-Party Tracker Inventory"
    # Hand-written VIOLATION_META must still win where it exists.
    assert titles["cookie_violation"] == "Cookie Consent Banner"


def test_penalty_block_cites_a_real_provision():
    """The penalty map was keyed by a stale vocabulary matching NO emitted slug,
    so every finding in every report rendered 'Consult legal counsel'."""
    for d in _details():
        assert d["penalty"]["legislation"] != "PDPA 2012 (general provisions)", d["type"]
        assert d["penalty"]["reference"] != "Consult legal counsel", d["type"]


def test_penalty_and_legislation_cannot_disagree():
    """Two maps meant a finding could cite one section in its Legislation line and
    another in its Penalty block."""
    svc = _svc()
    for d in _details():
        assert d["penalty"]["legislation"] == svc._get_violation_legislation(d["type"])[0]


def test_marketing_penalty_is_per_message_not_the_pdpa_ceiling():
    """Unsolicited marketing is penalised per message under the Spam Control Act.
    Quoting the S$1M PDPA ceiling overstates the customer's exposure."""
    pen = get_penalty_for_violation("marketing_violation")
    assert "per message" in pen["amount"]
    assert "1,000,000" not in pen["amount"]


def test_unknown_slug_does_not_invent_a_section():
    """An unmapped slug must degrade honestly, never guess a section number."""
    pen = get_penalty_for_violation("some_future_violation")
    assert pen["legislation"] == "PDPA 2012 (general provisions)"
    assert pen["reference"] == "Consult legal counsel"
