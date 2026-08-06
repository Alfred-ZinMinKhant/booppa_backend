"""Unit tests for Week 1 GTM components: scan consent, address detector, UBO graph."""

import pytest

from app.services.address_screen_detector import normalize_address, screen_addresses
from app.services.scan_consent import has_active_consent
from app.services.ubo_graph_builder import (
    Entity,
    Relationship,
    build_graph,
    find_shared_control_patterns,
    from_csp_records,
    graph_to_visualization_json,
)


def test_has_active_consent_requires_id():
    with pytest.raises(ValueError, match="exactly one of csp_client_id or managed_entity_id"):
        has_active_consent(None, required_source="acra_lookup")


def test_has_active_consent_invalid_source():
    with pytest.raises(ValueError, match="unknown required_source"):
        has_active_consent(None, csp_client_id="00000000-0000-0000-0000-000000000001", required_source="invalid_source")


def test_normalize_address():
    raw = "1 Raffles Place, #20-61, Level 20, Singapore 048616"
    norm = normalize_address(raw)
    assert "#20-61" not in norm
    assert "level" not in norm
    assert "1 raffles place" in norm


def test_address_screen_detector():
    sample_vendors = [
        {"vendor_name": "Alpha", "uen": "U001", "address": "1 Raffles Place, #20-61, Singapore 048616"},
        {"vendor_name": "Beta", "uen": "U002", "address": "1 Raffles Place, #20-05, Singapore 048616"},
        {"vendor_name": "Gamma", "uen": "U003", "address": "Regus, 80 Robinson Road, Singapore 068898"},
    ]
    signals = screen_addresses(sample_vendors)
    assert len(signals) == 3

    # Gamma matches known provider "regus"
    gamma_signal = [s for s in signals if s.vendor_name == "Gamma"][0]
    assert gamma_signal.known_provider_match == "regus"
    assert gamma_signal.shared_address_signal is True


def test_ubo_graph_builder():
    company_rows = [
        {"uen": "C001", "name": "Alpha Corp"},
        {"uen": "C002", "name": "Beta Corp"},
    ]
    director_rows = [
        {"nominee_full_name": "John Tan", "nominator_name": "Alice Lee", "company_uen": "C001"},
        {"nominee_full_name": "John Tan", "nominator_name": "Alice Lee", "company_uen": "C002"},
    ]
    shareholder_rows = []
    ubo_rows = []

    g = from_csp_records(director_rows, shareholder_rows, ubo_rows, company_rows)
    assert g.number_of_nodes() > 0

    patterns = find_shared_control_patterns(g, min_shared_companies=2)
    assert len(patterns) >= 1
    names = [p["person_name"] for p in patterns]
    assert "John Tan" in names or "Alice Lee" in names

    viz = graph_to_visualization_json(g)
    assert "nodes" in viz and "edges" in viz
