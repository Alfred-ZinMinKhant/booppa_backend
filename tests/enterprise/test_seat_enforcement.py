"""Seat-enforcement regression tests.

Pins behaviour:
  - Buyer Starter org gets max_seats=1; second invite returns 402.
  - Pending invites count toward the cap.
  - max_seats_for() returns the right value per plan.
  - /seats endpoint returns expected shape.
"""
from __future__ import annotations

import uuid

import pytest

from app.billing.enforcement import max_seats_for

from tests._test_helpers import make_user, make_org, auth_headers


# ── max_seats_for() lookup table ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan,expected",
    [
        ("buyer_starter", 1),
        ("buyer_starter_monthly", 1),
        ("buyer_starter_annual", 1),
        ("buyer_pro", 3),
        ("buyer_pro_monthly", 3),
        ("buyer_pro_annual", 3),
        ("buyer_enterprise", None),       # unlimited
        ("buyer_enterprise_monthly", None),
        ("standard_suite", None),
        ("pro_suite", None),
        ("enterprise", None),             # legacy
        ("enterprise_pro", None),
        # Unknown / free defaults to single seat (matches new-org policy)
        ("free", 1),
        ("", 1),
    ],
)
def test_max_seats_for_lookup(plan, expected):
    assert max_seats_for(plan) == expected


# ── /seats endpoint ──────────────────────────────────────────────────────────


def test_seats_endpoint_summary_shape(client, test_db):
    owner = make_user(test_db, plan="buyer_pro_monthly", role="PROCUREMENT")
    org = make_org(test_db, owner=owner, max_seats=3)
    resp = client.get(
        f"/api/v1/enterprise/organisations/{org.id}/seats",
        headers=auth_headers(owner),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["used"] == 1               # owner is the only active member
    assert body["pending_invites"] == 0
    assert body["limit"] == 3
    assert body["remaining"] == 2


# ── Invite blocking at the cap ───────────────────────────────────────────────


def test_buyer_starter_blocks_second_invite(client, test_db):
    """Starter org has max_seats=1 — the owner already takes the seat, so the
    very first invite should be blocked. (Procurement.create_invite test
    pre-empted by the collaboration gate — Starter is excluded.)"""
    owner = make_user(test_db, plan="buyer_starter_monthly", role="PROCUREMENT")
    org = make_org(test_db, owner=owner, max_seats=1)
    resp = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/invites",
        headers=auth_headers(owner),
        json={"email": "teammate@booppa.io", "role": "member"},
    )
    # Collaboration gate fires first (Starter excluded) → 402.
    assert resp.status_code == 402


def test_buyer_pro_blocks_fourth_invite(client, test_db):
    """Pro org cap is 3. Owner + 2 invites = 3 → the third invite (4th total)
    should be blocked. Pending invites count toward the cap."""
    owner = make_user(test_db, plan="buyer_pro_monthly", role="PROCUREMENT")
    org = make_org(test_db, owner=owner, max_seats=3)
    h = auth_headers(owner)

    # 2 invites — should succeed (owner=1 + invite=1 + invite=2 = 3 = cap)
    for i in range(2):
        r = client.post(
            f"/api/v1/enterprise/organisations/{org.id}/invites",
            headers=h,
            json={"email": f"teammate-{i}@booppa.io", "role": "member"},
        )
        assert r.status_code == 201, f"invite #{i+1} got {r.status_code}: {r.text[:200]}"

    # 3rd invite — would push us to 4 seats, exceeds cap.
    r3 = client.post(
        f"/api/v1/enterprise/organisations/{org.id}/invites",
        headers=h,
        json={"email": "teammate-overflow@booppa.io", "role": "member"},
    )
    assert r3.status_code == 402
    assert "seat" in r3.json().get("detail", "").lower()


def test_buyer_enterprise_seats_unlimited(client, test_db):
    """Buyer Enterprise has no seat cap — many invites should all succeed."""
    owner = make_user(test_db, plan="buyer_enterprise_monthly", role="PROCUREMENT")
    org = make_org(test_db, owner=owner, max_seats=None)
    h = auth_headers(owner)
    # Send 8 invites — well above any of the lower-tier caps.
    for i in range(8):
        r = client.post(
            f"/api/v1/enterprise/organisations/{org.id}/invites",
            headers=h,
            json={"email": f"teammate-{i}@booppa.io", "role": "member"},
        )
        assert r.status_code == 201, f"unlimited invite #{i+1} returned {r.status_code}"
