"""Regression lock for the recurring "Privacy Policy = Google's policy" bug.

The RFP homepage scraper (`RFPExpressBuilder._fetch_website_context`) harvests the
first ``privacy/pdpa/data-protection`` link on the page. A third-party widget
(cookie banner, embed) whose "Privacy" link points at ``policies.google.com`` used
to win when it appeared in the DOM before the vendor's own link, so the kit shipped
"Privacy Policy: https://policies.google.com/privacy" for the vendor.

Fix under test: a same-domain guard (`_same_site`) rejects off-domain matches and
keeps scanning until a link on the vendor's own domain (or a relative link) is found.

These tests drive the REAL scraper with a mocked HTTP client — no network — so the
guard is exercised end to end, not a reimplementation of it.
"""
import asyncio

import pytest

from app.services.rfp_express_builder import RFPExpressBuilder


class _FakeResp:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status


class _FakeClient:
    """Minimal async httpx.AsyncClient stand-in. Serves HTML for the base URL and
    404s every other page the scraper probes (/about, /privacy-policy, /privacy)."""

    def __init__(self, html_by_url: dict):
        self._html = html_by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if url in self._html:
            return _FakeResp(self._html[url], 200)
        return _FakeResp("", 404)


@pytest.fixture
def _no_cache(monkeypatch):
    """Force a cache miss + no-op write so the scraper always runs the fetch path."""
    from app.core.cache import cache as cache_mod
    monkeypatch.setattr(cache_mod, "get", lambda *a, **k: None)
    monkeypatch.setattr(cache_mod, "set", lambda *a, **k: None)


def _scrape(monkeypatch, html: str, vendor_url: str = "https://acme.sg") -> dict:
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient({vendor_url.rstrip("/"): html}))
    builder = RFPExpressBuilder(vendor_id="vendor@acme.sg", vendor_email="vendor@acme.sg")
    return asyncio.run(builder._fetch_website_context(vendor_url))


# A homepage where a third-party (Google) privacy link appears BEFORE the vendor's
# own — the exact DOM order that produced the bug.
_HTML_GOOGLE_THEN_VENDOR = """
<html><body>
  <p>{padding}</p>
  <a href="https://policies.google.com/privacy?hl=en-US">Google Privacy</a>
  <a href="https://acme.sg/privacy-policy">Our Privacy Policy</a>
</body></html>
""".format(padding="Acme Pte Ltd provides security services. " * 20)

_HTML_ONLY_GOOGLE = """
<html><body>
  <p>{padding}</p>
  <a href="https://policies.google.com/privacy?hl=en-US">Google Privacy</a>
</body></html>
""".format(padding="Acme Pte Ltd provides security services. " * 20)

_HTML_RELATIVE = """
<html><body>
  <p>{padding}</p>
  <a href="/legal/privacy">Privacy Policy</a>
</body></html>
""".format(padding="Acme Pte Ltd provides security services. " * 20)


def test_offdomain_google_link_is_skipped_for_vendor_link(_no_cache, monkeypatch):
    result = _scrape(monkeypatch, _HTML_GOOGLE_THEN_VENDOR)
    assert result["privacy_policy_url"] == "https://acme.sg/privacy-policy"
    assert "google.com" not in (result["privacy_policy_url"] or "")


def test_only_offdomain_link_yields_no_privacy_url(_no_cache, monkeypatch):
    """If the ONLY candidate is off-domain, we must return None rather than
    fabricate a foreign privacy policy for the vendor."""
    result = _scrape(monkeypatch, _HTML_ONLY_GOOGLE)
    assert result["privacy_policy_url"] is None


def test_relative_link_is_resolved_against_vendor_domain(_no_cache, monkeypatch):
    result = _scrape(monkeypatch, _HTML_RELATIVE)
    assert result["privacy_policy_url"] == "https://acme.sg/legal/privacy"
