"""Headless-render fallback for WAF-blocked sites.

Incident 2026-07-27: a paid PDPA Quick Scan for point-star.com died because
Cloudflare answered 403 to both httpx User-Agents and `_scan_site_metadata`
gave up. Real Chrome usually passes where httpx does not, and Playwright is
already in the worker image.

The critical invariant under test is NOT "rendering succeeds" — it is that a
render which returns a *challenge page* must still read inaccessible. Scoring a
bot-challenge page would reproduce the false-compliance failure the
accessibility gate exists to prevent.
"""
import asyncio
import sys

import pytest

from app.workers import tasks as tasks_mod


def _run(coro):
    # asyncio.run, not get_event_loop: other suites in a full run leave the
    # ambient loop closed, which fails these tests for reasons unrelated to them.
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    """Stands in for the httpx.AsyncClient from get_async_client."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


_REAL_PAGE = (
    "<html><body>" + ("Our privacy policy explains how we handle your data. " * 12)
    + "</body></html>"
)

_CHALLENGE_PAGE = (
    "<html><body>Checking your browser before accessing. "
    "cloudflare ray id: 8f2a1b</body></html>"
)


@pytest.fixture
def block_httpx(mocker):
    """Every httpx attempt returns 403, as a WAF-fronted site does."""
    client = _FakeClient(_FakeResponse(status_code=403, text="Forbidden"))
    mocker.patch.object(tasks_mod, "get_async_client", return_value=client)
    return client


# ── The rescue ───────────────────────────────────────────────────────────────

def test_playwright_rescues_a_403(block_httpx, mocker):
    """403 from httpx + a readable render == an accessible site."""
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(
            return_value=(_REAL_PAGE, 200, {"strict-transport-security": "max-age=31536000"})
        ),
    )

    result = _run(tasks_mod._scan_site_metadata("https://waf.example"))

    assert result["site_accessible"] is True
    assert result["http_status"] == 200
    assert result["render_method"] == "playwright"
    # The original block is preserved for diagnostics.
    assert result["httpx_blocked_status"] == 403
    assert "site_inaccessible_reason" not in result


def test_rescue_reports_the_rendered_headers_not_the_403s(block_httpx, mocker):
    """Security headers must come from the response we actually read.

    Carrying the blocked 403's headers forward would report HSTS as absent when
    it is merely unobserved — absence of evidence rendered as evidence.
    """
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(
            return_value=(
                _REAL_PAGE,
                200,
                {
                    "strict-transport-security": "max-age=31536000",
                    "content-security-policy": "default-src 'self'",
                },
            )
        ),
    )

    result = _run(tasks_mod._scan_site_metadata("https://waf.example"))

    assert result["security_headers"]["hsts"] is True
    assert result["security_headers"]["csp"] is True
    assert result["security_headers"]["x_frame_options"] is False


def test_connection_reset_is_also_rescuable(mocker):
    """Some WAFs reset the connection instead of answering 403.

    That lands in the except branch, so the fallback must live outside the try.
    """
    client = _FakeClient(raises=Exception("Connection reset by peer"))
    mocker.patch.object(tasks_mod, "get_async_client", return_value=client)
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(return_value=(_REAL_PAGE, 200, {})),
    )

    result = _run(tasks_mod._scan_site_metadata("https://reset.example"))

    assert result["site_accessible"] is True
    # A successful render supersedes the transport error.
    assert "scan_error" not in result


# ── The guardrail ────────────────────────────────────────────────────────────

def test_rendered_challenge_page_is_still_inaccessible(block_httpx, mocker):
    """THE invariant: rendering a bot-challenge is not reaching the site."""
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(return_value=(_CHALLENGE_PAGE, 200, {})),
    )

    result = _run(tasks_mod._scan_site_metadata("https://waf.example"))

    assert result["site_accessible"] is False
    assert result.get("render_method") is None
    assert "403" in result["site_inaccessible_reason"]


def test_render_returning_an_error_status_is_not_a_rescue(block_httpx, mocker):
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(return_value=("<html>nope</html>", 403, {})),
    )

    result = _run(tasks_mod._scan_site_metadata("https://waf.example"))

    assert result["site_accessible"] is False


def test_no_findings_are_produced_for_an_unrescued_site(block_httpx, mocker):
    """Body checks must not run against content we never retrieved."""
    mocker.patch.object(
        tasks_mod,
        "_render_html_via_playwright",
        new=mocker.AsyncMock(return_value=(None, 0, {})),
    )

    result = _run(tasks_mod._scan_site_metadata("https://waf.example"))

    assert result["site_accessible"] is False
    # The keys the PDPA findings engine reads must be absent entirely, not False.
    assert "privacy_policy" not in result
    assert "consent_mechanism" not in result


# ── Cost control ─────────────────────────────────────────────────────────────

def test_a_healthy_site_never_pays_the_render_cost(mocker):
    """A clean 200 must not invoke Playwright at all."""
    client = _FakeClient(_FakeResponse(status_code=200, text=_REAL_PAGE))
    mocker.patch.object(tasks_mod, "get_async_client", return_value=client)
    render = mocker.patch.object(
        tasks_mod, "_render_html_via_playwright", new=mocker.AsyncMock()
    )

    result = _run(tasks_mod._scan_site_metadata("https://healthy.example"))

    assert result["site_accessible"] is True
    render.assert_not_awaited()


# ── API-image degradation ────────────────────────────────────────────────────

def test_missing_playwright_degrades_silently(mocker):
    """The API image has no playwright (kept out for CVE surface).

    `app/integrations/scan1/adapter.py` calls this from the API process, so an
    ImportError must be a no-op, not a 500.
    """
    mocker.patch.dict(sys.modules, {"playwright.async_api": None, "playwright": None})

    html, status, headers = _run(
        tasks_mod._render_html_via_playwright("https://any.example")
    )

    assert (html, status, headers) == (None, 0, {})


def test_render_failure_is_swallowed(mocker):
    """A crashing browser must not fail the scan — it degrades to the httpx verdict."""
    class _Boom:
        def __call__(self):
            raise RuntimeError("chromium unavailable")

    mocker.patch.dict(
        sys.modules, {"playwright.async_api": type(sys)("playwright.async_api")}
    )
    sys.modules["playwright.async_api"].async_playwright = _Boom()

    html, status, headers = _run(
        tasks_mod._render_html_via_playwright("https://any.example")
    )

    assert (html, status, headers) == (None, 0, {})


# ── Header helper ────────────────────────────────────────────────────────────

def test_security_headers_helper_is_case_insensitive():
    assert tasks_mod._security_headers_from(
        {"Strict-Transport-Security": "max-age=1"}
    )["hsts"] is True
    assert tasks_mod._security_headers_from({})["hsts"] is False
