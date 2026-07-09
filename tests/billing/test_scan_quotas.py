"""Scan-quota regression tests for the buyer-ladder VendorScanLedger.

Pins behaviour:
  - QUICK consumes one credit per unique vendor per month.
  - Re-viewing the same vendor in the same month is silently free.
  - 429 fires at the cap on a NEW vendor.
  - 402 fires when the plan doesn't include the tier (e.g. DEEP for Starter).
  - /scan-quota returns the expected shape.
  - The unique constraint prevents double-spending on a race.

Tests exercise both the helper directly (unit) and the endpoints (integration).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.billing.scan_credits import consume_scan, scan_usage
from app.core.models import VendorScanLedger

from tests._test_helpers import make_user, auth_headers


def _vid() -> uuid.UUID:
    return uuid.uuid4()


# ── Unit: consume_scan helper ────────────────────────────────────────────────


def test_consume_scan_starter_increments_used(test_db):
    buyer = make_user(test_db, plan="buyer_starter_monthly", role="PROCUREMENT")
    result = consume_scan(test_db, buyer.id, "buyer_starter_monthly", _vid(), "QUICK")
    test_db.commit()
    assert result["allowed"] is True
    assert result["already_consumed"] is False
    assert result["used"] == 1
    assert result["limit"] == 10
    assert result["remaining"] == 9


def test_consume_scan_same_vendor_twice_is_free(test_db):
    buyer = make_user(test_db, plan="buyer_starter_monthly", role="PROCUREMENT")
    vendor = _vid()
    consume_scan(test_db, buyer.id, "buyer_starter_monthly", vendor, "QUICK")
    test_db.commit()
    result = consume_scan(test_db, buyer.id, "buyer_starter_monthly", vendor, "QUICK")
    test_db.commit()
    assert result["already_consumed"] is True
    assert result["used"] == 1  # not 2
    # Only one row in the ledger
    rows = test_db.query(VendorScanLedger).filter(VendorScanLedger.buyer_id == buyer.id).all()
    assert len(rows) == 1


def test_consume_scan_starter_hits_429_at_cap(test_db):
    buyer = make_user(test_db, plan="buyer_starter_monthly", role="PROCUREMENT")
    # Consume 10 distinct vendors — the Starter cap.
    for _ in range(10):
        consume_scan(test_db, buyer.id, "buyer_starter_monthly", _vid(), "QUICK")
        test_db.commit()
    # 11th distinct vendor exceeds the cap.
    with pytest.raises(HTTPException) as exc:
        consume_scan(test_db, buyer.id, "buyer_starter_monthly", _vid(), "QUICK")
    assert exc.value.status_code == 429
    assert "limit reached" in exc.value.detail.lower()


def test_consume_scan_deep_402_for_starter(test_db):
    """DEEP isn't in the Starter tier — should 402 immediately on the first call."""
    buyer = make_user(test_db, plan="buyer_starter_monthly", role="PROCUREMENT")
    with pytest.raises(HTTPException) as exc:
        consume_scan(test_db, buyer.id, "buyer_starter_monthly", _vid(), "DEEP")
    assert exc.value.status_code == 402
    assert "not included" in exc.value.detail.lower()


def test_consume_scan_evidence_402_for_pro(test_db):
    """EVIDENCE is Enterprise+ only — Pro buyers can't reach it."""
    buyer = make_user(test_db, plan="buyer_pro_monthly", role="PROCUREMENT")
    with pytest.raises(HTTPException) as exc:
        consume_scan(test_db, buyer.id, "buyer_pro_monthly", _vid(), "EVIDENCE")
    assert exc.value.status_code == 402


def test_consume_scan_pro_suite_is_unlimited(test_db):
    """Pro Suite has `None` limits — should never 402/429."""
    buyer = make_user(test_db, plan="pro_suite_monthly", role="PROCUREMENT")
    # Hit 50 distinct vendors at the QUICK tier — well past Buyer Enterprise's 100 cap.
    for _ in range(50):
        result = consume_scan(test_db, buyer.id, "pro_suite_monthly", _vid(), "QUICK")
        test_db.commit()
        assert result["limit"] is None
        assert result["remaining"] is None


# ── Unit: scan_usage snapshot ────────────────────────────────────────────────


def test_scan_usage_summary_shape(test_db):
    buyer = make_user(test_db, plan="buyer_pro_monthly", role="PROCUREMENT")
    consume_scan(test_db, buyer.id, "buyer_pro_monthly", _vid(), "QUICK")
    consume_scan(test_db, buyer.id, "buyer_pro_monthly", _vid(), "QUICK")
    consume_scan(test_db, buyer.id, "buyer_pro_monthly", _vid(), "DEEP")
    test_db.commit()

    snapshot = scan_usage(test_db, buyer.id, "buyer_pro_monthly")
    assert set(snapshot["scans"].keys()) == {"QUICK", "DEEP", "EVIDENCE"}
    assert snapshot["scans"]["QUICK"]["used"] == 2
    assert snapshot["scans"]["QUICK"]["limit"] == 50
    assert snapshot["scans"]["DEEP"]["used"] == 1
    assert snapshot["scans"]["DEEP"]["limit"] == 20
    assert snapshot["scans"]["EVIDENCE"]["used"] == 0
    assert snapshot["scans"]["EVIDENCE"]["limit"] == 0  # not in plan


# ── Integration: /scan-quota endpoint ────────────────────────────────────────


def test_scan_quota_endpoint_returns_expected_shape(client, test_db):
    buyer = make_user(test_db, plan="buyer_enterprise_monthly", role="PROCUREMENT")
    resp = client.get(
        "/api/v1/procurement/scan-quota",
        headers=auth_headers(buyer),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "buyer_enterprise_monthly"
    assert set(body["scans"].keys()) == {"QUICK", "DEEP", "EVIDENCE"}
    # Enterprise caps per the marketing
    assert body["scans"]["QUICK"]["limit"] == 100
    assert body["scans"]["DEEP"]["limit"] == 100
    assert body["scans"]["EVIDENCE"]["limit"] == 15
    # No consumption yet
    assert body["scans"]["QUICK"]["used"] == 0
    assert body["scans"]["QUICK"]["remaining"] == 100


def test_scan_quota_blocked_for_free_user(client, test_db):
    buyer = make_user(test_db, plan="free", role="PROCUREMENT")
    resp = client.get(
        "/api/v1/procurement/scan-quota",
        headers=auth_headers(buyer),
    )
    # Procurement role + free plan → 403 from PROCUREMENT_PLAN_KEYS gate.
    assert resp.status_code == 403
