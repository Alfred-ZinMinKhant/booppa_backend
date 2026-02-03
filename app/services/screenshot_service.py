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

    # Final fallback: public screenshot service (no API key required).
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"https://image.thum.io/get/width/1400/{url}",
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.content
            logger.warning(
                f"Thum.io screenshot failed for {url}: {resp.status_code} {resp.text}"
            )
    except Exception as e:
        logger.warning(f"Thum.io screenshot attempt failed for {url}: {e}")

    return None


def capture_screenshot_base64(url: str) -> Optional[str]:
    b = capture_screenshot_bytes(url)
    if not b:
        return None
    return base64.b64encode(b).decode()
