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


def _is_placeholder(resp: httpx.Response) -> bool:
    """Return True if the response looks like a placeholder / error image."""
    final = str(resp.url)
    return "mshots/v1/default" in final or "mshots/v1/0" in final


def capture_screenshot_bytes(url: str, timeout: int = 60) -> Optional[bytes]:
    """Return PNG/JPEG bytes for a screenshot of `url`, or None if all providers fail."""

    # ── 1. Playwright ──────────────────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            # Wait 60s for splash screens / animated intros to finish
            page.wait_for_timeout(60000)
            img = page.screenshot(full_page=False)
            browser.close()
            logger.info(f"Screenshot via Playwright for {url}")
            return img
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
                    if resp.status_code == 200 and len(resp.content) > _MIN_REAL_BYTES:
                        logger.info(f"Screenshot via Browserless for {url}")
                        return resp.content
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Browserless attempt failed for {url}: {e}")

    # ── 3–6. Public HTTP providers ─────────────────────────────────────────────
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:

        # ── 3. Microlink ───────────────────────────────────────────────────────
        # Returns JSON with a CDN URL to the actual screenshot — real, immediate.
        try:
            api = f"https://api.microlink.io?url={quote_plus(url)}&screenshot=true&meta=false"
            resp = client.get(api)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    img_url = (data.get("data") or {}).get("screenshot", {}).get("url")
                    if img_url:
                        img_resp = client.get(img_url)
                        if img_resp.status_code == 200 and len(img_resp.content) > _MIN_REAL_BYTES:
                            logger.info(f"Screenshot via Microlink for {url}")
                            return img_resp.content
        except Exception as e:
            logger.warning(f"Microlink screenshot failed for {url}: {e}")

        # ── 4. Thum.io ────────────────────────────────────────────────────────
        try:
            resp = client.get(f"https://image.thum.io/get/width/1400/{url}")
            if resp.status_code == 200 and len(resp.content) > _MIN_REAL_BYTES:
                logger.info(f"Screenshot via Thum.io for {url}")
                return resp.content
            logger.warning(f"Thum.io failed for {url}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Thum.io error for {url}: {e}")

        # ── 5. mshots (with retry) ────────────────────────────────────────────
        # First request queues the screenshot; subsequent requests return the real image.
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
                if resp.status_code == 200 and len(resp.content) > _MIN_REAL_BYTES:
                    logger.info(f"Screenshot via mshots (attempt {attempt+1}) for {url}")
                    return resp.content
                break
            except Exception as e:
                logger.warning(f"mshots attempt {attempt+1} failed for {url}: {e}")
                break

        # ── 6. Screenshot.guru ────────────────────────────────────────────────
        try:
            resp = client.get(
                f"https://screenshot.guru/api?url={quote_plus(url)}&width=1400"
            )
            if resp.status_code == 200 and len(resp.content) > _MIN_REAL_BYTES:
                logger.info(f"Screenshot via Screenshot.guru for {url}")
                return resp.content
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
