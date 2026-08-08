"""Stripe live/test mode must be stated, and the credentials must agree.

Production ran on `sk_test_` from launch to 2026-08-08 without anyone noticing,
because every observable signal is identical in test mode — checkout completes,
the webhook fires, entitlement is granted, the kit is delivered. Only the money
is absent (AUDIT_2026-08-08.md P0-D).

Staying in test mode is fine. The dangerous state is a half-finished switch, so
these tests pin the detection of each half-migrated pairing.
"""
import pytest

from app.billing import stripe_mode as sm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_MODE"):
        monkeypatch.delenv(key, raising=False)


# ── mode detection ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,expected", [
    ("sk_test_abc123", sm.TEST),
    ("sk_live_abc123", sm.LIVE),
    ("rk_live_abc123", sm.LIVE),
    ("", sm.UNKNOWN),
    (None, sm.UNKNOWN),
    ("price_1TbhIVHOrR48Mz6b", sm.UNKNOWN),  # price ids carry no mode marker
])
def test_mode_is_read_from_the_credential(key, expected):
    """The key states the mode; never infer it from an env flag."""
    assert sm._mode_of(key) == expected


def test_unset_secret_key_is_reported(monkeypatch):
    problems = sm.check_consistency()
    assert any("STRIPE_SECRET_KEY is unset" in p for p in problems)


# ── the half-migrated states ──────────────────────────────────────────────────

def test_declared_live_but_test_key_is_caught(monkeypatch):
    """Someone set STRIPE_MODE=live and forgot the secret."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    monkeypatch.setenv("STRIPE_MODE", "live")
    problems = sm.check_consistency()
    assert any("says 'live'" in p and "test key" in p for p in problems)


def test_declared_test_but_live_key_is_caught(monkeypatch):
    """The direction that charges real cards by accident."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    monkeypatch.setenv("STRIPE_MODE", "test")
    problems = sm.check_consistency()
    assert any("says 'test'" in p and "live key" in p for p in problems)


def test_missing_webhook_secret_is_caught(monkeypatch):
    """The silent killer: payment succeeds, fulfilment never runs."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    problems = sm.check_consistency()
    assert any("STRIPE_WEBHOOK_SECRET is unset" in p for p in problems)
    assert any("never fulfilled" in p for p in problems)


def test_consistent_config_has_no_problems(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    monkeypatch.setenv("STRIPE_MODE", "test")
    assert sm.check_consistency() == []


def test_undeclared_mode_does_not_fail(monkeypatch):
    """STRIPE_MODE is optional — existing deployments must not break on rollout."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    assert sm.check_consistency() == []


# ── visibility ────────────────────────────────────────────────────────────────

def test_startup_banner_names_the_mode(monkeypatch, caplog):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    with caplog.at_level("WARNING"):
        sm.log_startup_banner()
    assert "TEST MODE" in caplog.text
    assert "no real money" in caplog.text


def test_startup_banner_is_loud_about_live(monkeypatch, caplog):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    with caplog.at_level("WARNING"):
        sm.log_startup_banner()
    assert "LIVE MODE" in caplog.text
    assert "real cards will be charged" in caplog.text


def test_health_reports_mode_without_leaking_the_key(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_supersecretvalue")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    body = client.get("/api/v1/health").json()
    assert body["stripe"]["mode"] == "test"
    assert "supersecretvalue" not in str(body)
