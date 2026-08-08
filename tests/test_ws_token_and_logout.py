"""Coverage for the WS handshake token and real single-session logout.

Two audit findings meet here:

P1-5 — `/api/ws-token` documented itself as returning a SHORT-LIVED token and
actually returned the raw 7-day session JWT. Anything that captured it (XSS, a
logged handshake) got a week of full API access. The token is now `type: "ws"`,
expires in ~2 minutes, and is rejected by `verify_access_token`.

Logout — clearing cookies was the entire "logout". The refresh token stayed
valid server-side for 30 days, so a captured token outlived the sign-out the
user was shown. `/auth/logout` now revokes the presented token, while `/revoke`
remains the separate "sign out everywhere" control.
"""
from datetime import timedelta

import pytest

from app.core.auth import (
    create_access_token,
    create_refresh_token,
    create_ws_token,
    verify_access_token,
    verify_refresh_token,
    verify_ws_token,
)

EMAIL = "ws-user@booppa.io"


# ── Token scoping ───────────────────────────────────────────────────────────

def test_ws_token_is_not_accepted_as_an_access_token():
    """The whole point of the type discriminator."""
    ws = create_ws_token(EMAIL)
    assert verify_ws_token(ws)["sub"] == EMAIL
    assert verify_access_token(ws) is None
    assert verify_refresh_token(ws) is None


def test_access_token_is_not_accepted_as_a_ws_token():
    """A client cannot skip the exchange and hand the socket its session JWT."""
    access = create_access_token({"sub": EMAIL})
    assert verify_ws_token(access) is None


def test_ws_token_is_actually_short_lived():
    """A token that never expires would defeat the exchange entirely."""
    expired = create_ws_token(EMAIL, expires_delta=timedelta(seconds=-1))
    assert verify_ws_token(expired) is None


def test_ws_token_default_lifetime_is_minutes_not_days():
    from jose import jwt as _jwt
    from app.core.config import settings

    payload = _jwt.decode(
        create_ws_token(EMAIL), settings.SECRET_KEY, algorithms=["HS256"]
    )
    lifetime = payload["exp"] - payload["iat"]
    assert 0 < lifetime <= 300, f"ws token lives {lifetime}s — far too long for a handshake"


# ── The exchange endpoint ───────────────────────────────────────────────────

def test_ws_token_endpoint_requires_authentication(client):
    assert client.get("/api/v1/auth/ws-token").status_code == 401


def test_ws_token_endpoint_rejects_a_refresh_token(client):
    refresh = create_refresh_token({"sub": EMAIL})
    r = client.get(
        "/api/v1/auth/ws-token", headers={"Authorization": f"Bearer {refresh}"}
    )
    assert r.status_code == 401


def test_ws_token_endpoint_returns_a_scoped_token(client):
    access = create_access_token({"sub": EMAIL})
    r = client.get(
        "/api/v1/auth/ws-token", headers={"Authorization": f"Bearer {access}"}
    )
    assert r.status_code == 200
    minted = r.json()["wsToken"]

    # It must not simply echo the session token back — that was the bug.
    assert minted != access
    assert verify_ws_token(minted)["sub"] == EMAIL
    assert verify_access_token(minted) is None


# ── Logout actually revokes ─────────────────────────────────────────────────

def test_logout_revokes_the_presented_refresh_token(client):
    from app.api.auth import _store_token, _token_exists

    refresh = create_refresh_token({"sub": EMAIL})
    _store_token(refresh, EMAIL)
    assert _token_exists(refresh)

    r = client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert r.status_code == 204
    assert not _token_exists(refresh), "signed out, but the refresh token still works"


def test_logout_is_idempotent_and_never_fails_the_user(client):
    """A logout that errors leaves the user thinking they're still signed in."""
    for body in ({}, {"refresh_token": ""}, {"refresh_token": "not-a-jwt"}):
        assert client.post("/api/v1/auth/logout", json=body).status_code == 204


def test_refresh_tokens_are_unique_per_session(client):
    """Two sessions must never share one credential.

    The payload was {sub, exp, type} and `exp` has one-second resolution, so two
    logins in the same second minted the identical JWT — one credential for two
    devices, and revoking either revoked both.
    """
    a = create_refresh_token({"sub": EMAIL})
    b = create_refresh_token({"sub": EMAIL})
    assert a != b
    assert verify_refresh_token(a)["jti"] != verify_refresh_token(b)["jti"]


def test_logout_does_not_sign_the_user_out_of_other_devices(client):
    """Routine logout must not behave like /revoke."""
    from app.api.auth import _store_token, _token_exists

    phone = create_refresh_token({"sub": EMAIL})
    laptop = create_refresh_token({"sub": EMAIL})
    _store_token(phone, EMAIL)
    _store_token(laptop, EMAIL)

    client.post("/api/v1/auth/logout", json={"refresh_token": laptop})

    assert not _token_exists(laptop)
    assert _token_exists(phone), "logging out one device killed the user's other sessions"
