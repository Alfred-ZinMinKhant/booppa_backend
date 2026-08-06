"""Comprehensive test suite for all 14 GTM components."""

import pytest

from app.services.address_screen_detector import screen_addresses
from app.services.buyer_dashboard_cascade import (
    CascadeSkipReason,
    build_cascade_notification_text,
    run_buyer_vendor_cascade,
)
from app.services.cyber_risk_signal import build_cyber_risk_signal, query_shodan_internetdb
from app.services.mas_tprm_export import (
    Materiality,
    VendorArrangement,
    calculate_provider_concentration,
    classify_materiality,
    from_vendor_records,
)
from app.services.my_reality_check_service import build_reality_check
from app.services.scan_consent import has_active_consent
from app.services.trust_passport_service import assemble_l2_passport_data, generate_l1_passport
from app.services.ubo_graph_builder import from_csp_records
from app.services.vendor_challenger import ChallengeRequest, validate_and_build_challenge


def test_scan_consent_validation():
    with pytest.raises(ValueError):
        has_active_consent(None, required_source="acra_lookup")


def test_address_screening():
    vendors = [
        {"vendor_name": "A", "uen": "U1", "address": "1 Raffles Place #10-01"},
        {"vendor_name": "B", "uen": "U2", "address": "1 Raffles Place #10-05"},
    ]
    results = screen_addresses(vendors)
    assert len(results) == 2
    assert results[0].shared_with_count == 1


def test_ubo_graph_construction():
    companies = [{"uen": "C1", "name": "Company One"}]
    directors = [{"nominee_full_name": "John Doe", "nominator_name": "Jane Doe", "company_uen": "C1"}]
    g = from_csp_records(directors, [], [], companies)
    assert g.number_of_nodes() > 0


def test_cyber_risk_signal_structure(mocker):
    mocker.patch("socket.gethostbyname", return_value="127.0.0.1")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 404
    mocker.patch("requests.get", return_value=mock_resp)

    res = query_shodan_internetdb("127.0.0.1")
    assert "found" in res
    sig = build_cyber_risk_signal("example.com")
    assert sig["resolved"] is True
    assert sig["signal_level"] == "minimal_data"


def test_mas_tprm_exporter():
    records = [
        {"vendor_name": "Cloud Vendor", "label": "cloud hosting data processing"},
        {"vendor_name": "Cloud Vendor", "label": "cloud backup data processing"},
    ]
    arrangements = from_vendor_records(records, buyer_org_id="org1", register_period="2026-H2")
    assert len(arrangements) == 2
    assert arrangements[0].materiality == Materiality.MATERIAL


def test_reality_check():
    sector_data = [
        {"compliance_score": 50, "visibility_score": 40, "engagement_score": 30, "recency_score": 30, "procurement_interest_score": 20, "total_score": 170},
        {"compliance_score": 80, "visibility_score": 70, "engagement_score": 60, "recency_score": 60, "procurement_interest_score": 50, "total_score": 320},
    ]
    res = build_reality_check("v1", "Tech", sector_data[0], sector_data)
    assert res.vendor_total_score == 170
    assert len(res.top_gaps) <= 3


def test_trust_passport_l1_and_l2():
    l1 = generate_l1_passport({"vendor_id": "v1", "company_name": "Test Co"}, "https://booppa.io")
    assert l1.tier == "L1"

    deep_scan_sample = {
        "scan_id": "s1",
        "dimensions": [{"name": "Encryption", "status": "PASS"}],
    }
    l2_data = assemble_l2_passport_data(deep_scan_sample, {"vendor_id": "v1", "company_name": "Test Co"})
    assert l2_data["tier"] == "L2"
    assert l2_data["dimensions_failing"] == 0


def test_vendor_challenger_composition():
    req = ChallengeRequest(
        vendor_uen="202012345A",
        vendor_company_name="Vendor A",
        vendor_user_id="u1",
        buyer_email="buyer@example.com",
    )
    res = validate_and_build_challenge(req, "https://booppa.io")
    assert "https://booppa.io/passport/profile/202012345A" in res["passport_url"]
    assert "Vendor A" in res["subject"]


def test_buyer_dashboard_cascade_authorization_gate():
    class DummyDB:
        def query(self, *args, **kwargs):
            return self
        def filter(self, *args, **kwargs):
            return self
        def exists(self, *args, **kwargs):
            return self
        def scalar(self, *args, **kwargs):
            return False  # No active authorization

    db = DummyDB()
    results = run_buyer_vendor_cascade(db, "buyer1", "Buyer Co", dry_run=True)
    assert len(results) == 1
    assert results[0].skip_reason == CascadeSkipReason.NO_AUTHORIZATION


def test_cyber_risk_signal_consent_gate():
    class DummyDB:
        def query(self, *args, **kwargs):
            return self
        def filter(self, *args, **kwargs):
            return self
        def exists(self, *args, **kwargs):
            return self
        def scalar(self, *args, **kwargs):
            return False  # Consent missing

    db = DummyDB()
    sig = build_cyber_risk_signal("example.com", db=db, csp_client_id="00000000-0000-0000-0000-000000000001")
    assert sig["resolved"] is False
    assert sig["consent_active"] is False
    assert "consent missing or revoked" in sig["signal_basis"]


def test_passport_router_prefixes():
    from app.api.passport_public import router as public_router
    from app.api.trust_passport_api import router as api_router

    assert public_router.prefix == "/passport"
    assert api_router.prefix == "/passport"


def test_trust_passport_monitor_task_registered():
    from app.workers.celery_app import celery_app
    from app.workers.tasks import run_trust_passport_monitor_reanchors

    assert "run_trust_passport_monitor_reanchors" in celery_app.tasks
    assert "trust-passport-monitor-reanchor-daily" in celery_app.conf.beat_schedule


def test_reality_check_route_order():
    from app.api.trust_passport_api import router as api_router

    paths = [getattr(r, "path", "") for r in api_router.routes]
    reality_idx = paths.index("/passport/reality-check")
    uen_idx = paths.index("/passport/{vendor_uen}")
    assert reality_idx < uen_idx, "/reality-check route must precede /{vendor_uen} catch-all"


