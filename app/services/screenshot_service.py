import logging
import base64
from typing import Optional

logger = logging.getLogger(__name__)
import os
import httpx


def capture_screenshot_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    """Capture a full-page screenshot of `url` using Playwright (sync). Returns PNG bytes or None.

    This function imports Playwright at runtime; ensure `playwright` is installed and browsers
    are installed in the runtime (`playwright install`). It is safe to fail and return None.
    """
    # Try Playwright first (fast, local). If it fails, fall back to an HTTP screenshot service.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000)
            # Wait for banner to render (some banners take a second to pop up)
            page.wait_for_timeout(2000) 
            img = page.screenshot(full_page=True)
            browser.close()
            return img
    except Exception as e:
        logger.warning(f"Playwright screenshot capture failed for {url}: {e}")

    # Fallback: browserless HTTP API (container `browserless` at port 3000)
    browserless_url = os.environ.get("BROWSERLESS_URL", "http://browserless:3000")
    try:
        with httpx.Client(timeout=timeout) as client:
            # Try POST with options wrapper (some browserless variants expect nested options)
            try:
                resp = client.post(
                    f"{browserless_url}/screenshot",
                    json={"url": url, "options": {"fullPage": True}},
                    follow_redirects=True,
                )
                if resp.status_code == 200:
                    return resp.content
            except Exception:
                # swallow and continue to other attempts
                pass

            # Try POST with minimal body
            try:
                resp = client.post(
                    f"{browserless_url}/screenshot",
                    json={"url": url},
                    follow_redirects=True,
                )
                if resp.status_code == 200:
                    return resp.content
            except Exception:
                pass

            # Try GET with query param as a last resort
            try:
                resp = client.get(
                    f"{browserless_url}/screenshot",
                    params={"url": url},
                    follow_redirects=True,
                )
                if resp.status_code == 200:
                    return resp.content
                else:
                    logger.warning(
                        f"Browserless screenshot failed for {url}: {resp.status_code} {resp.text}"
                    )
            except Exception as e:
                logger.warning(f"Browserless screenshot attempt failed for {url}: {e}")
    except Exception as e:
        logger.warning(f"Browserless screenshot attempt failed for {url}: {e}")

    # Final fallback chain: public screenshot services (no API key required).
    from urllib.parse import quote_plus

    providers = [
        # WordPress mshots — free, fetches from their own servers (most reliable)
        (f"https://s.wordpress.com/mshots/v1/{quote_plus(url)}?w=1400", "mshots"),
        # Thum.io
        (f"https://image.thum.io/get/width/1400/{url}", "thum.io"),
        # Screenshot.guru
        (f"https://screenshot.guru/api?url={quote_plus(url)}&width=1400", "screenshot.guru"),
    ]
    with httpx.Client(timeout=timeout) as client:
        for endpoint_url, name in providers:
            try:
                resp = client.get(endpoint_url, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 2000:
                    logger.info(f"Screenshot captured via {name} for {url}")
                    return resp.content
                logger.warning(f"{name} screenshot failed for {url}: {resp.status_code}")
            except Exception as e:
                logger.warning(f"{name} screenshot attempt failed for {url}: {e}")

    return None


def capture_screenshot_base64(url: str) -> Optional[str]:
    b = capture_screenshot_bytes(url)
    if not b:
        return None
    return base64.b64encode(b).decode()
