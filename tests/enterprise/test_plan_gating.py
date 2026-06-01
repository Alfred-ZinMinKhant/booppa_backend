"""Plan-gating regression tests for the post-2026-06-01 enforcement changes.

Three gates covered:

  1. Procurement endpoints (procurement.py, rfp_requirements.py) — accept the
     full enterprise set PLUS Buyer Starter; reject VENDOR-role users; reject
     free-tier procurement accounts.
  2. Suite endpoints (enterprise_api.py) — TRM/webhooks/retention require
     Standard or Pro Suite; SSO/white-label/subsidiaries require Pro Suite.
  3. Collaboration endpoints (watchlist, invites) — require Buyer Pro+ or any
     Suite/Enterprise plan. Buyer Starter is intentionally excluded.

These pin the wiring in enforcement.py::{PROCUREMENT,SUITE,PRO_SUITE,
COLLABORATION}_PLAN_KEYS — silent drift fails fast.
"""
from __future__ import annotations

import pytest

from tests._test_helpers import make_user, make_org, auth_headers


# ── Procurement gating ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan",
    [
        "buyer_starter",          # newly admitted by Fix #2
        "buyer_pro",
        "buyer_enterprise",
        "standard_suite",
        "pro_suite",
        "enterprise",             # legacy
        "enterprise_pro",         # legacy
    ],
)
def test_procurement_vendors_accepts_paying_buyers(client, test_db, plan):
    user = make_user(test_db, plan=plan, role="PROCUREMENT")
    resp = client.get(
        "/api/v1/procurement/vendors",
        headers=auth_headers(user),
    )
    # 200 (list returned) is the green path. 404 / 5xx would indicate wiring breaks.
    assert resp.status_code == 200, f"plan={plan} got {resp.status_code}: {resp.text[:200]}"


def test_procurement_vendors_rejects_vendor_role(client, test_db):
    """Only PROCUREMENT (or ADMIN) accounts can hit procurement endpoints."""
    user = make_user(test_db, plan="buyer_pro", role="VENDOR")
    resp = client.get(
        "/api/v1/procurement/vendors",
        headers=auth_headers(user),
    )
    assert resp.status_code == 403
    assert "procurement account" in resp.json().get("detail", "").lower()


def test_procurement_vendors_rejects_free_procurement(client, test_db):
    user = make_user(test_db, plan="free", role="PROCUREMENT")
    resp = client.get(
        "/api/v1/procurement/vendors",
        headers=auth_headers(user),
    )
    assert resp.status_code == 403


# ── Suite gating ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan,expected",
    [
        ("free", 402),
        ("buyer_pro", 402),                 # buyer plans don't reach TRM
        ("standard_suite", 200),
        ("standard_suite_monthly", 200),
        ("pro_suite", 200),
        ("enterprise_monthly", 200),        # legacy grandfathered
        ("enterprise_pro_monthly", 200),    # legacy grandfathered
    ],
)
def test_trm_controls_require_suite_plan(client, test_db, plan, expected):
    owner = make_user(test_db, plan=plan, role="PROCUREMENT")
    org = make_org(test_db, owner=owner, tier="standard")
    resp = client.get(
        f"/api/v1/enterprise/organisations/{org.id}/trm",
        headers=auth_headers(owner),
    )
    assert resp.status_code == expected, f"plan={plan} got {resp.status_code}"


@pytest.mark.parametrize(
    "plan,expected",
    [
        ("standard_suite_monthly", 402),   # Standard CAN'T reach Pro features
        ("pro_suite_monthly", 200),
        ("enterprise_pro_monthly", 200),   # legacy GOVERNMENT-tier
        ("buyer_enterprise_monthly", 402),
        ("free", 402),
    ],
)
def test_white_label_requires_pro_suite(client, test_db, plan, expected):
    """White-label config is gated to Pro Suite only."""
    owner = make_user(test_db, plan=plan, role="PROCUREMENT")
    org = make_org(test_db, owner=owner)
    resp = client.put(
        f"/api/v1/enterprise/organisations/{org.id}/white-label",
        headers=auth_headers(owner),
        json={"primary_color": "#10b981"},
    )
    assert resp.status_code == expected, f"plan={plan} got {resp.status_code}"


def test_trm_gap_analysis_requires_suite_plan_to_block_deepseek_cost_leak(
    client, test_db, monkeypatch
):
    """Free-tier user should NOT be able to trigger a DeepSeek-billed call."""
    # Patch the DeepSeek call so this test never hits the network even if the
    # gate fails. The assertion is on the 402, not on the DeepSeek side-effect.
    from app.trm_workflow_service import run_gap_analysis as real_run
    monkeypatch.setattr(
        "app.api.enterprise_api.run_gap_analysis",
        lambda *a, **kw: None,
        raising=False,
    )

    owner = make_user(test_db, plan="free", role="PROCUREMENT")
    org = make_org(test_db, owner=owner)
    resp = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/trm/gap-analysis",
        headers=auth_headers(owner),
        json={"control_id": "00000000-0000-0000-0000-000000000000", "context": ""},
    )
    assert resp.status_code == 402
    assert "subscription required" in resp.json().get("detail", "").lower()


# ── Collaboration gating ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan,expected",
    [
        ("free", 402),
        ("buyer_starter", 402),             # single-seat by design
        ("buyer_pro", 201),
        ("buyer_enterprise", 201),
        ("standard_suite", 201),
        ("pro_suite", 201),
        ("enterprise", 201),                # legacy
    ],
)
def test_create_invite_requires_collaboration_plan(client, test_db, plan, expected):
    owner = make_user(test_db, plan=plan, role="PROCUREMENT")
    # Give multi-seat plans enough headroom so the seat-limit gate doesn't
    # shadow the collaboration gate.
    org = make_org(test_db, owner=owner, max_seats=None if plan != "buyer_pro" else 3)
    resp = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/invites",
        headers=auth_headers(owner),
        json={"email": f"teammate-{plan}@booppa.io", "role": "member"},
    )
    assert resp.status_code == expected, f"plan={plan} got {resp.status_code}: {resp.text[:200]}"


def test_watchlist_create_requires_collaboration_plan(client, test_db):
    owner = make_user(test_db, plan="buyer_starter", role="PROCUREMENT")
    org = make_org(test_db, owner=owner)
    resp = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/watchlist",
        headers=auth_headers(owner),
        json={"vendor_ref": "acme-co", "vendor_name": "ACME"},
    )
    assert resp.status_code == 402
    assert "team-collaboration" in resp.json().get("detail", "").lower()


def test_watchlist_create_allowed_for_buyer_pro(client, test_db):
    owner = make_user(test_db, plan="buyer_pro", role="PROCUREMENT")
    org = make_org(test_db, owner=owner)
    resp = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/watchlist",
        headers=auth_headers(owner),
        json={"vendor_ref": "acme-co", "vendor_name": "ACME"},
    )
    assert resp.status_code == 201
