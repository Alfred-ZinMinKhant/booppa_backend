"""
tests/test_government_portal_auth.py

Regression guard for AUDIT_2026-08-07.md P0-1.

Before 2026-08-07 the government portal had three compounding defects:
  1. `app/api/government.py` imported no auth at all — every data endpoint was
     readable by anyone on the internet.
  2. The `.gov.sg` domain check on /register was commented out "for testing",
     so any address could mint a role="GOVERNMENT" account.
  3. The public landing page stated the .gov.sg restriction as fact.

These tests pin (1) and (2). If someone reintroduces a public branch here, the
landing-page copy becomes false again — so this file failing is a compliance
signal, not just a test failure.
"""

import uuid

import pytest

from app.core.auth import create_access_token, get_password_hash
from app.core.models import User


def _uniq(prefix: str, domain: str) -> str:
    """Unique address per case — the users table is not truncated between
    parametrized runs, so a fixed address collides on ix_users_email."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}@{domain}"


# Every endpoint that serves portal data. Registration and login are public by
# design and are covered separately below.
GOV_DATA_ENDPOINTS = [
    ("get", "/api/government/vendors"),
    ("get", "/api/government/vendors/T05LL1234A"),
    ("get", "/api/government/tenders"),
    ("get", "/api/government/stats"),
    ("post", "/api/government/verify"),
    ("post", "/api/government/shortlist-report"),
]


def _call(client, method, path, **kw):
    return client.get(path, **kw) if method == "get" else client.post(path, json={}, **kw)


def _make_user(db, email, role):
    user = User(
        email=email,
        hashed_password=get_password_hash("Passw0rd!123"),
        company="Test Agency",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _bearer(email):
    return {"Authorization": f"Bearer {create_access_token(data={'sub': email})}"}


@pytest.mark.parametrize("method,path", GOV_DATA_ENDPOINTS)
def test_data_endpoints_reject_anonymous(client, method, path):
    """No portal data is readable without authentication."""
    assert _call(client, method, path).status_code == 401


@pytest.mark.parametrize("method,path", GOV_DATA_ENDPOINTS)
def test_data_endpoints_reject_non_government_role(client, test_db, method, path):
    """A valid Booppa login is not enough — the GOVERNMENT role is required."""
    email = _uniq("vendor", "example.com")
    _make_user(test_db, email, "VENDOR")
    r = _call(client, method, path, headers=_bearer(email))
    assert r.status_code == 403


@pytest.mark.parametrize("method,path", GOV_DATA_ENDPOINTS)
def test_data_endpoints_reject_gov_role_on_non_gov_email(client, test_db, method, path):
    """Defence against accounts minted while the domain check was disabled.

    The role column alone must not grant access — an account created during the
    window when /register accepted any address still carries role=GOVERNMENT.
    """
    email = _uniq("attacker", "gmail.com")
    _make_user(test_db, email, "GOVERNMENT")
    r = _call(client, method, path, headers=_bearer(email))
    assert r.status_code == 403


def test_register_rejects_non_gov_email(client):
    """The gate that the landing page promises exists, enforced server-side."""
    r = client.post(
        "/api/government/register",
        json={"email": "attacker@gmail.com", "password": "Passw0rd!123"},
    )
    assert r.status_code == 422
    assert ".gov.sg" in r.json()["detail"]


@pytest.mark.parametrize(
    "email",
    [
        "officer@agency.gov.sg.attacker.com",  # suffix must be terminal
        "officer@notgov.sg",                   # near-miss domain
        "officer@gov.sg.co",                   # trailing TLD
    ],
)
def test_register_rejects_lookalike_domains(client, email):
    r = client.post(
        "/api/government/register", json={"email": email, "password": "Passw0rd!123"}
    )
    assert r.status_code == 422


def test_register_accepts_gov_email(client):
    r = client.post(
        "/api/government/register",
        json={
            "email": _uniq("officer", "agency.gov.sg"),
            "password": "Passw0rd!123",
            "full_name": "Test Officer",
            "agency": "Test Agency",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "GOVERNMENT"
    assert body["access_token"]


def test_registered_gov_user_can_read_portal_data(client):
    """End-to-end: register → token → data endpoint returns 200, not 401/403."""
    reg = client.post(
        "/api/government/register",
        json={"email": _uniq("officer2", "agency.gov.sg"), "password": "Passw0rd!123"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    r = client.get(
        "/api/government/vendors", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
