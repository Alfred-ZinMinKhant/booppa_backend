"""Unit tests for unclaimed vendor Quick Scan, demo score drop hydration, and copy consistency."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from app.services.supplier_due_diligence_generator import build_certificate_data


def test_build_certificate_data_hydrates_demo_metrics_for_meridian():
    """Meridian Logistics demo supplier must populate score drop (Trust Score 44, delta -14)."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None  # No real user

    data = build_certificate_data(
        db,
        buyer_user_id="test-buyer-id",
        vendor_ref="demo-meridian-logistics-pte-ltd",
        vendor_name="Meridian Logistics Pte Ltd",
        sample_data=True,
    )

    assert data["resolved"] is True
    assert data["sample_data"] is True
    assert data["supplier_name"] == "Meridian Logistics Pte Ltd"
    assert "trust_score" in data
    assert data["trust_score"] is not None
    assert data["trust_delta"] < 0  # Demonstrates trust score drop!


@pytest.mark.asyncio
async def test_perform_unclaimed_vendor_quick_scan_runs_all_three_checks():
    """`perform_unclaimed_vendor_quick_scan` composes ACRA, MAS Prohibition Orders, and PDPA scan."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    # NOTE: screen_entity and run_free_scan are SYNCHRONOUS. They must be patched
    # with plain MagicMocks so this test fails if the endpoint ever awaits them
    # directly again instead of bridging through the threadpool.
    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity") as mock_screen, \
         patch("app.services.pdpa_free_scan_service.run_free_scan") as mock_pdpa:

        mock_acra.return_value = {
            "found": True,
            "live": True,
            "entity_status": "REGISTERED",
            "registered_name": "Test Unclaimed Vendor Pte Ltd",
            "uen": "202199999Z",
            "entity_type": "EXEMPT PRIVATE COMPANY LIMITED BY SHARES",
        }
        mock_screen_res = MagicMock()
        mock_screen_res.is_clear = True
        mock_screen_res.lists_checked = ["OFAC SDN", "UN Consolidated", "EU Consolidated", "MAS Prohibition Orders & Sanctions"]
        mock_screen_res.to_dict.return_value = {"is_clear": True}
        mock_screen.return_value = mock_screen_res

        # run_free_scan's real contract: "score" is a RISK score (higher = worse).
        mock_pdpa.return_value = {
            "website_url": "https://testvendor.example",
            "score": 12,
            "risk_level": "Low Risk",
            "total_findings": 1,
        }

        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "202199999Z", website="https://testvendor.example")

        assert res["company"] == "Test Unclaimed Vendor Pte Ltd"
        assert res["uen"] == "202199999Z"
        assert res["acraStatus"] == "REGISTERED"
        assert res["sanctionsScreening"]["isClear"] is True
        assert res["sanctionsScreening"]["masProhibitionOrdersChecked"] is True
        assert res["pdpaRiskScore"] == 12
        assert res["pdpaFlag"] == "CLEAN"

        mock_acra.assert_called_once()
        mock_screen.assert_called_once_with("Test Unclaimed Vendor Pte Ltd")
        mock_pdpa.assert_called_once()


@pytest.mark.asyncio
async def test_quick_scan_high_pdpa_risk_is_not_reported_clean():
    """A high PDPA RISK score must flag NEEDS_ATTENTION, not CLEAN."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity") as mock_screen, \
         patch("app.services.pdpa_free_scan_service.run_free_scan") as mock_pdpa:
        mock_acra.return_value = {"found": True, "live": True, "registered_name": "Risky Pte Ltd"}
        mock_screen.return_value = MagicMock(is_clear=True, lists_checked=["OFAC SDN"])
        mock_pdpa.return_value = {"score": 72, "risk_level": "High Risk", "total_findings": 6}

        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "Risky Pte Ltd", website="risky.example")
        assert res["pdpaFlag"] == "NEEDS_ATTENTION"
        # World-Check absent → MAS coverage must not be claimed.
        assert res["sanctionsScreening"]["masProhibitionOrdersChecked"] is False


@pytest.mark.asyncio
async def test_quick_scan_failed_sanctions_screen_is_unscreened_not_clean():
    """A screening that could not run must never be reported as a clear screening."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity", side_effect=RuntimeError("provider down")), \
         patch("app.services.pdpa_free_scan_service.run_free_scan") as mock_pdpa:
        mock_acra.return_value = {"found": True, "live": True, "registered_name": "Opaque Pte Ltd"}
        mock_pdpa.return_value = None

        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "Opaque Pte Ltd")

        assert res["sanctionsScreening"]["status"] == "UNSCREENED"
        assert res["sanctionsScreening"]["isClear"] is None
        assert res["sanctionsScreening"]["masProhibitionOrdersChecked"] is False
        assert res["riskSignal"] == "UNKNOWN"


def _fresh_dv(payload, age_days=0):
    from datetime import datetime, timedelta, timezone
    dv = MagicMock()
    dv.source_data = {
        "quick_scan": payload,
        "scanned_at": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
    }
    return dv


@pytest.mark.asyncio
async def test_quick_scan_reuses_cached_result_without_external_calls():
    """A re-check inside the cache window must not re-run ACRA/sanctions/PDPA."""
    db = MagicMock()
    cached_payload = {"company": "Cached Pte Ltd", "pdpaFlag": "CLEAN"}
    db.query.return_value.filter.return_value.first.return_value = _fresh_dv(cached_payload, age_days=1)

    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity") as mock_screen, \
         patch("app.services.pdpa_free_scan_service.run_free_scan") as mock_pdpa:
        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "Cached Pte Ltd")

        assert res["company"] == "Cached Pte Ltd"
        # Cached results are labelled as such — never presented as a fresh scan.
        assert res["cached"] is True
        assert res["scannedAt"]
        mock_acra.assert_not_called()
        mock_screen.assert_not_called()
        mock_pdpa.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days,refresh", [(30, False), (1, True)])
async def test_quick_scan_rescans_when_stale_or_refresh_forced(age_days, refresh):
    """A stale cache entry, or refresh=True, must re-run the live checks."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _fresh_dv(
        {"company": "Old Pte Ltd"}, age_days=age_days
    )

    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity") as mock_screen, \
         patch("app.services.pdpa_free_scan_service.run_free_scan") as mock_pdpa:
        mock_acra.return_value = {"found": True, "live": True, "registered_name": "Old Pte Ltd"}
        mock_screen.return_value = MagicMock(is_clear=True, lists_checked=["OFAC SDN"])
        mock_pdpa.return_value = None

        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "Old Pte Ltd", refresh=refresh)

        assert res["cached"] is False
        mock_acra.assert_called_once()
        mock_screen.assert_called_once()


@pytest.mark.asyncio
async def test_quick_scan_undateable_cache_is_a_miss():
    """A cache entry we cannot date is a cache we cannot vouch for."""
    db = MagicMock()
    dv = MagicMock()
    dv.source_data = {"quick_scan": {"company": "Undated Pte Ltd"}, "scanned_at": "not-a-timestamp"}
    db.query.return_value.filter.return_value.first.return_value = dv

    with patch("app.services.evidence_enricher.fetch_acra_status", new_callable=AsyncMock) as mock_acra, \
         patch("app.services.csp_sanctions.screen_entity") as mock_screen, \
         patch("app.services.pdpa_free_scan_service.run_free_scan"):
        mock_acra.return_value = {"found": True, "live": True, "registered_name": "Undated Pte Ltd"}
        mock_screen.return_value = MagicMock(is_clear=True, lists_checked=[])

        from app.api.procurement import perform_unclaimed_vendor_quick_scan

        res = await perform_unclaimed_vendor_quick_scan(db, "Undated Pte Ltd")
        assert res["cached"] is False
        mock_acra.assert_called_once()


def test_sanctions_copy_does_not_name_mas_unless_actually_checked():
    """Customer-facing copy must not claim MAS coverage the screen didn't have."""
    from app.services.csp_sanctions import sanctions_coverage_label, MAS_LIST_LABEL

    assert "MAS" not in sanctions_coverage_label([])
    assert "MAS" not in sanctions_coverage_label(["OFAC SDN", "UN Consolidated"])
    assert "MAS Prohibition Orders" in sanctions_coverage_label(["OFAC SDN", MAS_LIST_LABEL])
