"""
Screenshot service
==================
Priority chain:
  1. Playwright (local chromium — best quality, requires `playwright install chromium` on server)
  2. Browserless (self-hosted container at BROWSERLESS_URL)
  3. Microlink  (free public API, real screenshots, ~50 req/day free tier, no API key)
  4. Thum.io    (free, may 502 under load)
  5. mshots     (WordPress CDN — queues async; retried up to 3× with 4 s delay)
  6. Screenshot.guru (last resort)

Fix: mshots returns a "Generating Preview…" placeholder on first request for new URLs.
We detect the redirect to /default and retry with delay, or fall through.
"""

import base64
import logging
import os
import time
from typing import Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

_MIN_REAL_BYTES = 8_000   # anything smaller is likely a placeholder or error page

# Playwright settle wait after networkidle. Previously 60_000ms which exceeded
# every caller's outer timeout — meaning Playwright never actually completed and
# we always fell through to public providers that return HTML masquerades. 3 s
# is enough for typical splash animations on modern SPAs.
_PLAYWRIGHT_SETTLE_MS = 3_000
_PLAYWRIGHT_NAV_TIMEOUT_MS = 25_000


def _is_placeholder(resp: httpx.Response) -> bool:
    """Return True if the response looks like a placeholder / error image."""
    final = str(resp.url)
    return "mshots/v1/default" in final or "mshots/v1/0" in final


def looks_like_image(b: Optional[bytes]) -> bool:
    """Magic-byte sniff for PNG / JPEG / WebP / GIF.

    Defends against providers that fall back to returning HTML (e.g. their own
    marketing/error page) when they can't reach the target. Such bytes would
    otherwise be base64-encoded and rendered inside <img src=data:image/png...>
    in the report viewer, producing the "unstyled marketing page in the
    screenshot slot" bug.
    """
    if not b or len(b) < 12:
        return False
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if b[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return True
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def _accept(provider: str, url: str, body: bytes) -> Optional[bytes]:
    """Return body if it's plausibly a real image; otherwise log and reject."""
    if len(body) <= _MIN_REAL_BYTES:
        logger.warning(f"{provider} returned {len(body)} bytes (<{_MIN_REAL_BYTES}) for {url}")
        return None
    if not looks_like_image(body):
        head = body[:16].hex()
        logger.warning(
            f"{provider} returned non-image bytes for {url} (first16=0x{head}) — likely HTML; rejecting"
        )
        return None
    return body


def capture_screenshot_bytes(url: str, timeout: int = 45) -> Optional[bytes]:
    """Return PNG/JPEG bytes for a screenshot of `url`, or None if all providers fail."""

    # ── 1. Playwright ──────────────────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=_PLAYWRIGHT_NAV_TIMEOUT_MS, wait_until="networkidle")
            # Brief settle wait so animated intros render their final frame.
            page.wait_for_timeout(_PLAYWRIGHT_SETTLE_MS)
            img = page.screenshot(full_page=False)
            browser.close()
            if looks_like_image(img):
                logger.info(f"Screenshot via Playwright for {url}")
                return img
            logger.warning(f"Playwright returned non-image bytes for {url} — falling through")
    except Exception as e:
        logger.warning(f"Playwright screenshot failed for {url}: {e}")

    # ── 2. Browserless ─────────────────────────────────────────────────────────
    browserless_url = os.environ.get("BROWSERLESS_URL", "http://browserless:3000")
    try:
        with httpx.Client(timeout=timeout) as client:
            for body in (
                {"url": url, "options": {"fullPage": False}},
                {"url": url},
            ):
                try:
                    resp = client.post(f"{browserless_url}/screenshot",
                                       json=body, follow_redirects=True)
                    if resp.status_code == 200:
                        accepted = _accept("Browserless", url, resp.content)
                        if accepted:
                            logger.info(f"Screenshot via Browserless for {url}")
                            return accepted
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Browserless attempt failed for {url}: {e}")

    # ── 3–6. Public HTTP providers ─────────────────────────────────────────────
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:

        # ── 3. Microlink ───────────────────────────────────────────────────────
        try:
            api = f"https://api.microlink.io?url={quote_plus(url)}&screenshot=true&meta=false"
            resp = client.get(api)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    img_url = (data.get("data") or {}).get("screenshot", {}).get("url")
                    if img_url:
                        img_resp = client.get(img_url)
                        if img_resp.status_code == 200:
                            accepted = _accept("Microlink", url, img_resp.content)
                            if accepted:
                                logger.info(f"Screenshot via Microlink for {url}")
                                return accepted
        except Exception as e:
            logger.warning(f"Microlink screenshot failed for {url}: {e}")

        # ── 4. Thum.io ────────────────────────────────────────────────────────
        try:
            resp = client.get(f"https://image.thum.io/get/width/1400/{url}")
            if resp.status_code == 200:
                accepted = _accept("Thum.io", url, resp.content)
                if accepted:
                    logger.info(f"Screenshot via Thum.io for {url}")
                    return accepted
            else:
                logger.warning(f"Thum.io failed for {url}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Thum.io error for {url}: {e}")

        # ── 5. mshots (with retry) ────────────────────────────────────────────
        mshots = f"https://s.wordpress.com/mshots/v1/{quote_plus(url)}?w=1400"
        for attempt in range(4):
            try:
                resp = client.get(mshots)
                if _is_placeholder(resp):
                    if attempt < 3:
                        logger.info(f"mshots placeholder for {url}, retry {attempt+1}/3 after 4 s")
                        time.sleep(4)
                        continue
                    logger.warning(f"mshots still placeholder after retries for {url}")
                    break
                if resp.status_code == 200:
                    accepted = _accept("mshots", url, resp.content)
                    if accepted:
                        logger.info(f"Screenshot via mshots (attempt {attempt+1}) for {url}")
                        return accepted
                break
            except Exception as e:
                logger.warning(f"mshots attempt {attempt+1} failed for {url}: {e}")
                break

        # ── 6. Screenshot.guru ────────────────────────────────────────────────
        try:
            resp = client.get(
                f"https://screenshot.guru/api?url={quote_plus(url)}&width=1400"
            )
            if resp.status_code == 200:
                accepted = _accept("Screenshot.guru", url, resp.content)
                if accepted:
                    logger.info(f"Screenshot via Screenshot.guru for {url}")
                    return accepted
            else:
                logger.warning(f"Screenshot.guru failed for {url}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Screenshot.guru error for {url}: {e}")

    logger.warning(f"All screenshot providers failed for {url}")
    return None


def capture_screenshot_base64(url: str) -> Optional[str]:
    b = capture_screenshot_bytes(url)
    if not b:
        return None
    return base64.b64encode(b).decode()
