"""Sector-priority ordering of the MAS TRM domains (Sprint 9b)."""
from app.core.models_enterprise import MAS_TRM_DOMAINS
from app.services.trm_sector_override import (
    critical_domains,
    normalise_sector,
    reorder_controls_by_sector,
)


def _controls():
    return [{"domain": d, "control_ref": f"TRM-{i}"} for i, d in enumerate(MAS_TRM_DOMAINS, 1)]


def test_fintech_leads_with_governance_and_cyber():
    out = [c["domain"] for c in reorder_controls_by_sector(_controls(), "fintech")]
    assert out[:3] == [
        "Technology Risk Governance",
        "Cyber Security",
        "Data and Information Management",
    ]
    assert len(out) == len(MAS_TRM_DOMAINS)          # nothing dropped
    assert set(out) == set(MAS_TRM_DOMAINS)           # nothing duplicated


def test_healthcare_leads_with_data_and_incident():
    out = [c["domain"] for c in reorder_controls_by_sector(_controls(), "healthcare")]
    assert out[:2] == ["Data and Information Management", "Incident Management"]


def test_unknown_or_empty_sector_keeps_canonical_order():
    for sec in (None, "", "manufacturing"):
        out = [c["domain"] for c in reorder_controls_by_sector(_controls(), sec)]
        assert out == MAS_TRM_DOMAINS


def test_sector_aliases_normalise():
    assert normalise_sector("Banking") == "fintech"
    assert normalise_sector("financial services") == "fintech"
    assert normalise_sector("Medical") == "healthcare"
    assert normalise_sector("widgets") is None


def test_works_on_orm_like_objects():
    class _C:
        def __init__(self, domain):
            self.domain = domain
    rows = [_C(d) for d in MAS_TRM_DOMAINS]
    out = reorder_controls_by_sector(rows, "fintech")
    assert out[0].domain == "Technology Risk Governance"
    assert len(out) == len(MAS_TRM_DOMAINS)


def test_critical_domains_exposed_for_tagging():
    assert "Cyber Security" in critical_domains("fintech")
    assert critical_domains("manufacturing") == []
