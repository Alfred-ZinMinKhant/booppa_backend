"""Fixtures local to the CSP unit-test suite.

The pack's sanctions tests were authored in an environment without Redis, so the
in-module result cache was a no-op. Booppa's dev/CI environment has Redis up, and
several tests screen the same name ("John Smith") — the first (clear) result would
otherwise be cached and returned to a later test that patches the screeners to
return a hit. These are pure logic tests, not cache tests (the cache-key tests
exercise `_cache_key` directly), so disable the cache for the whole module.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_sanctions_cache(monkeypatch):
    import app.services.csp_sanctions as s
    monkeypatch.setattr(s, "_get_redis", lambda: None)
