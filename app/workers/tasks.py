from app.services.pdf_styles import get_unified_styles
from app.core.http_client import get_async_client
from .celery_app import celery_app
from celery.exceptions import Retry
from app.core.db import SessionLocal
from app.core.models import Report
from app.services.ai_service import AIService
from app.services.booppa_ai_service import BooppaAIService
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.storage import S3Service
from app.services.email_service import EmailService
from app.core.repositories.user_repository import UserRepository
from app.core.repositories.report_repository import ReportRepository
from app.services.screenshot_service import capture_screenshot_base64, looks_like_image
from app.core.config import settings
from app.billing.enforcement import enforce_tier
from app.services.audit_chain import append_audit_event
from app.services.dependency_logger import log_dependency_event
from app.services.verify_registry import register_verification
from app.integrations.ai.adapter import ai_preview
from app.services.ai_provider import DeepSeekProvider
from app.services.evidence_enricher import (
    fetch_hosting_signals,
    fetch_pdpc_enforcement,
    fetch_ssl_grade,
    fetch_acra_status,
)
from app.services.nric_classifier import (
    classify_candidates,
    find_valid_nric_values,
    harvest_candidates,
    summarise as summarise_nric,
)
from app.services.pdf_nric_scanner import scan_linked_pdfs
from app.services.policy_clause_classifier import (
    classify_clauses,
    classify_clauses_multilingual,
    harvest_clause_snippets,
    summarise as summarise_policy,
)
from app.services.pdpa_dimension_snapshot import compute_dimension_snapshots
from app.services.finding_keys import extract_finding_keys
from app.services.pdpa_findings import resolve_pdpa_findings, resolve_pdpa_score
import asyncio
import hashlib
import json
import logging
import httpx
import base64
import re
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _set_assessment_values(report: Report, updates: dict) -> None:
    if not isinstance(updates, dict):
        return
    try:
        if isinstance(report.assessment_data, dict):
            assessment = dict(report.assessment_data)
        else:
            assessment = {}
        assessment.update(updates)
        report.assessment_data = assessment
    except Exception as e:
        logger.warning(f"Failed to update assessment_data for {report.id}: {e}")


def _record_dimension_snapshots(db, report: Report) -> None:
    """Tier 4: persist one row per PDPA dimension to pdpa_dimension_history.

    Called from process_report_workflow after the metadata scan completes.
    Safe to call multiple times; each call writes a fresh batch with the same
    captured_at timestamp clustering.
    """
    from app.core.models import PdpaDimensionHistory

    snapshots = compute_dimension_snapshots(report.assessment_data)
    if not snapshots:
        return
    for snap in snapshots:
        db.add(PdpaDimensionHistory(
            vendor_id=report.owner_id,
            report_id=report.id,
            framework=report.framework or "pdpa_quick_scan",
            dimension_name=snap["dimension_name"],
            status=snap["status"],
            score=snap["score"],
        ))
    db.commit()


def _confirm_remediations(db, report: Report) -> None:
    """Tier 6: auto-confirm or regress any pending remediations for this vendor.

    For every FindingRemediation in (pending|regressed) state for this vendor+
    framework, check whether the corresponding finding_key still appears in
    the current scan. If gone → confirmation_status='confirmed'. If still
    present → 'regressed'. Idempotent — safe to call repeatedly.
    """
    from app.core.models import FindingRemediation

    current_keys = extract_finding_keys(report.assessment_data)
    pending = (
        db.query(FindingRemediation)
        .filter(
            FindingRemediation.vendor_id == report.owner_id,
            FindingRemediation.confirmation_status.in_(("pending", "regressed")),
            FindingRemediation.status.in_(("fixed", "wontfix")),
        )
        .all()
    )
    if not pending:
        return
    now = datetime.now(timezone.utc)
    for rem in pending:
        if rem.finding_key in current_keys:
            # Finding still appears — mark/keep as regressed
            rem.confirmation_status = "regressed"
        else:
            rem.confirmation_status = "confirmed"
            if not rem.confirmed_at:
                rem.confirmed_at = now
                rem.confirming_report_id = report.id
    db.commit()


async def _capture_screenshot_with_timeout(url: str, timeout: int = 45) -> str | None:
    """Run the screenshot_service chain off the event loop with a hard budget.

    Default 45 s allows the Playwright path (~25 s nav + 3 s settle + render)
    to actually complete on the first try. Previously this was 25 s, which is
    LESS than the Playwright path's internal wait — so Playwright was always
    killed and we always fell through to public providers that returned HTML.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(capture_screenshot_base64, url), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"Screenshot capture timed out for {url}")
        return None


async def _fetch_thum_io_base64(url: str, timeout: int = 30) -> tuple[str | None, str | None]:
    """
    Async screenshot fallback chain (used when Playwright/Browserless are unavailable).
    Order:
      1. Microlink  — real screenshots, free ~50/day, no API key
      2. Thum.io    — free tier
      3. mshots     — retried 3× with 4 s delay (first request queues the screenshot)
      4. Screenshot.guru — last resort

    Every branch now magic-byte-validates the response before encoding, so a
    provider returning an HTML error/marketing page is treated as a miss.
    """
    from urllib.parse import quote_plus
    _MIN = 8_000

    def _accept(provider: str, body: bytes) -> str | None:
        """Return base64 if body is a real image; else None and log why."""
        if len(body) <= _MIN:
            logger.warning(f"{provider} returned body of {len(body)} bytes (<{_MIN}) for {url}")
            return None
        if not looks_like_image(body):
            head = body[:16].hex()
            logger.warning(
                f"{provider} returned non-image bytes for {url} (first16=0x{head}) — likely HTML; rejecting"
            )
            return None
        return base64.b64encode(body).decode()

    async with get_async_client(timeout=timeout, follow_redirects=True) as client:

        # 1. Microlink — returns JSON with CDN screenshot URL
        try:
            api = f"https://api.microlink.io?url={quote_plus(url)}&screenshot=true&meta=false"
            resp = await client.get(api)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    img_url = (data.get("data") or {}).get("screenshot", {}).get("url")
                    if img_url:
                        img_resp = await client.get(img_url)
                        if img_resp.status_code == 200:
                            encoded = _accept("Microlink", img_resp.content)
                            if encoded:
                                logger.info(f"Screenshot via Microlink for {url}")
                                return encoded, None
        except Exception as e:
            logger.warning(f"Microlink failed for {url}: {e}")

        # 2. Thum.io
        try:
            resp = await client.get(f"https://image.thum.io/get/width/1400/{url}")
            if resp.status_code == 200:
                encoded = _accept("Thum.io", resp.content)
                if encoded:
                    logger.info(f"Screenshot via Thum.io for {url}")
                    return encoded, None
            else:
                logger.warning(f"Thum.io status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Thum.io error for {url}: {e}")

        # 3. mshots with retry (first request queues; retries get the real image)
        mshots = f"https://s.wordpress.com/mshots/v1/{quote_plus(url)}?w=1400"
        for attempt in range(4):
            try:
                resp = await client.get(mshots)
                final = str(resp.url)
                if "mshots/v1/default" in final or "mshots/v1/0" in final:
                    if attempt < 3:
                        logger.info(f"mshots placeholder for {url}, retry {attempt+1}/3 after 4 s")
                        await asyncio.sleep(4)
                        continue
                    logger.warning(f"mshots still placeholder after retries for {url}")
                    break
                if resp.status_code == 200:
                    encoded = _accept("mshots", resp.content)
                    if encoded:
                        logger.info(f"Screenshot via mshots (attempt {attempt+1}) for {url}")
                        return encoded, None
                break
            except Exception as e:
                logger.warning(f"mshots attempt {attempt+1} error for {url}: {e}")
                break

        # 4. Screenshot.guru
        try:
            resp = await client.get(
                f"https://screenshot.guru/api?url={quote_plus(url)}&width=1400"
            )
            if resp.status_code == 200:
                encoded = _accept("Screenshot.guru", resp.content)
                if encoded:
                    logger.info(f"Screenshot via Screenshot.guru for {url}")
                    return encoded, None
            else:
                logger.warning(f"Screenshot.guru status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Screenshot.guru error for {url}: {e}")

    return None, "all_providers_failed"


_TRACKER_DOMAINS: tuple[tuple[str, str], ...] = (
    # (substring, vendor label)
    ("google-analytics.com", "Google Analytics"),
    ("googletagmanager.com", "Google Tag Manager"),
    ("doubleclick.net", "Google Ads / DoubleClick"),
    ("googleadservices.com", "Google Ads"),
    ("facebook.com/tr", "Meta Pixel"),
    ("connect.facebook.net", "Meta Pixel"),
    ("hotjar.com", "Hotjar"),
    ("static.hotjar.com", "Hotjar"),
    ("mixpanel.com", "Mixpanel"),
    ("segment.io", "Segment"),
    ("segment.com", "Segment"),
    ("amplitude.com", "Amplitude"),
    ("fullstory.com", "FullStory"),
    ("clarity.ms", "Microsoft Clarity"),
    ("adobe.com/b/ss", "Adobe Analytics"),
    ("omtrdc.net", "Adobe Analytics"),
    ("licdn.com", "LinkedIn Insight Tag"),
    ("snap.licdn.com", "LinkedIn Insight Tag"),
    ("tiktok.com/i18n/pixel", "TikTok Pixel"),
    ("analytics.tiktok.com", "TikTok Pixel"),
    ("ads-twitter.com", "X (Twitter) Pixel"),
    ("static.ads-twitter.com", "X (Twitter) Pixel"),
    ("bat.bing.com", "Microsoft Advertising / Bing UET"),
    # ── Extended coverage (deterministic substring → vendor) ──────────────
    ("googlesyndication.com", "Google AdSense"),
    ("google.com/ads", "Google Ads"),
    ("region1.google-analytics.com", "Google Analytics 4"),
    ("analytics.google.com", "Google Analytics"),
    ("ct.pinterest.com", "Pinterest Tag"),
    ("s.pinimg.com", "Pinterest Tag"),
    ("redditstatic.com/ads", "Reddit Pixel"),
    ("pixel.reddit.com", "Reddit Pixel"),
    ("sc-static.net", "Snapchat Pixel"),
    ("tr.snapchat.com", "Snapchat Pixel"),
    ("criteo.com", "Criteo"),
    ("criteo.net", "Criteo"),
    ("taboola.com", "Taboola"),
    ("outbrain.com", "Outbrain"),
    ("quantserve.com", "Quantcast"),
    ("scorecardresearch.com", "Comscore"),
    ("yandex.ru/metrika", "Yandex Metrica"),
    ("mc.yandex.ru", "Yandex Metrica"),
    ("heapanalytics.com", "Heap"),
    ("pendo.io", "Pendo"),
    ("intercom.io", "Intercom"),
    ("intercomcdn.com", "Intercom"),
    ("crazyegg.com", "Crazy Egg"),
    ("optimizely.com", "Optimizely"),
    ("visualwebsiteoptimizer.com", "VWO"),
    ("js-agent.newrelic.com", "New Relic"),
    ("nr-data.net", "New Relic"),
    ("cloudflareinsights.com", "Cloudflare Web Analytics"),
    ("hs-analytics.net", "HubSpot"),
    ("hs-scripts.com", "HubSpot"),
    ("js.hsforms.net", "HubSpot"),
    ("pardot.com", "Salesforce Pardot"),
    ("munchkin.marketo.net", "Marketo"),
    ("matomo", "Matomo"),
    ("plausible.io", "Plausible"),
    ("cdn.segment.com", "Segment"),
    ("adsrvr.org", "The Trade Desk"),
    ("adnxs.com", "AppNexus / Xandr"),
    ("demdex.net", "Adobe Audience Manager"),
)


def _classify_tracker(request_url: str) -> str | None:
    """Return the vendor label if the request URL matches a known tracker."""
    url_lower = (request_url or "").lower()
    for needle, label in _TRACKER_DOMAINS:
        if needle in url_lower:
            return label
    return None


async def _detect_cookie_banner(url: str | None) -> dict:
    if not url:
        return {}

    indicators = [
        "cookiebot", "usercentrics", "cookieyes", "onetrust", "osano",
        "iubenda", "cookie-consent", "cookie-banner", "cookie banner",
        "cookie notice", "consentmanager", "data-cookieconsent",
        "booppa-cookie", "booppa_consent",
        "pdpa compliant", "pdpa-compliant",
        "optanon", "evidon", "didomi", "trustarc", "quantcast",
        "accept cookies", "allow cookies", "manage cookies",
        "we use cookies", "this site uses cookies", "cookie preferences",
        "cookie settings", "reject cookies",
    ]

    # Try Playwright for dynamic JS-rendered banners
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()

            # Tier 3: capture network requests pre-consent. Because the scanner
            # never clicks the banner, every tracker fired during page load is
            # by definition pre-consent.
            captured_requests: list[dict] = []

            def _on_request(req):
                try:
                    captured_requests.append({
                        "url": req.url,
                        "method": req.method,
                        "resource_type": req.resource_type,
                    })
                except Exception:
                    pass

            page.on("request", _on_request)

            await page.goto(url, timeout=45000, wait_until="networkidle")
            # Wait for splash screens / animated intros to finish (30s)
            # Many sites show loading animations with logos for 10-30s
            await asyncio.sleep(30)

            # Get full rendered HTML
            html = (await page.content()).lower()

            # Also check for common IDs/classes directly
            banner_visible = await page.evaluate("""() => {
                const selectors = [
                    '#booppa-cookie-banner', '.cookie-banner', '#cookie-banner',
                    '.cookie-consent', '#cookie-consent', '.cc-banner'
                ];
                return selectors.some(s => {
                    const el = document.querySelector(s);
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                });
            }""")

            await browser.close()

            # Build tracker inventory from captured requests
            tracker_hits: dict[str, list[str]] = {}
            for req in captured_requests:
                vendor = _classify_tracker(req["url"])
                if vendor:
                    tracker_hits.setdefault(vendor, []).append(req["url"])
            tracker_summary = {
                # All captured trackers are pre-consent because we never clicked
                # the banner. Schema reserves post_consent for future work.
                "pre_consent": [
                    {"vendor": v, "sample_url": urls[0], "count": len(urls)}
                    for v, urls in tracker_hits.items()
                ],
                "post_consent": [],
                "inventory": sorted(tracker_hits.keys()),
                "total_requests_captured": len(captured_requests),
            }

            found = [k for k in indicators if k in html]
            if found or banner_visible:
                return {
                    "consent_mechanism": {
                        "has_cookie_banner": True,
                        "has_active_consent": True,
                        "detected_providers": found,
                        "rendered_detection": True,
                        "pre_consent_trackers": tracker_summary["inventory"],
                    },
                    "trackers": tracker_summary,
                }
            else:
                # No banner found in DOM; still surface tracker inventory so
                # downstream scoring can flag pre-consent firings.
                return {
                    "trackers": tracker_summary,
                }
    except Exception as e:
        logger.warning(f"Playwright cookie detection failed, falling back to HTTP: {e}")

    # Fallback to static HTTP scan (browser-like headers to avoid 403)
    try:
        async with get_async_client(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=_BROWSER_UA_HEADERS)
            if resp.status_code == 403:
                resp = await client.get(url, headers={"User-Agent": "BooppaComplianceBot/1.0"})
            if resp.status_code >= 400:
                # Site not accessible — do NOT report "no banner found"
                return {"cookie_scan_error": f"http_{resp.status_code}"}
            html = resp.text.lower()
            if _is_loading_page(html):
                return {"cookie_scan_error": "loading_screen"}
            found = [k for k in indicators if k in html]
            if found:
                return {
                    "consent_mechanism": {
                        "has_cookie_banner": True,
                        "has_active_consent": True,
                        "detected_providers": found,
                    }
                }
            return {"consent_mechanism": {"has_cookie_banner": False}}
    except Exception as e:
        return {"cookie_scan_error": f"error:{str(e)[:200]}"}


_BROWSER_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_LOADING_SCREEN_PATTERNS = [
    "please wait", "loading...", "just a moment", "checking your browser",
    "one moment please", "verifying you are human", "please enable javascript",
    "attention required", "ray id", "ddos protection",
]

# These indicate the page is a real SPA / framework page, not a loading screen
_SPA_FRAMEWORK_SIGNALS = [
    "__next", "__nuxt", "react-root", "app-root", "ng-app",
    "<script src=", "<link rel=\"stylesheet\"", "<meta name=",
    "<!doctype html>",
]


def _is_loading_page(html: str) -> bool:
    """Detect bot-challenge / interstitial pages.

    Must NOT flag legitimate SPA shells (React, Next.js, Nuxt, etc.) that have
    minimal visible text but are real pages with JS-rendered content.
    Only flag pages that BOTH have very little content AND contain explicit
    loading/challenge keywords like 'checking your browser' or 'ray id'.
    """
    if not html or len(html.strip()) < 100:
        return True  # truly empty response
    html_lower = html.lower()

    # If the page contains SPA framework markers, it's a real page even if
    # visible text is sparse — JS will render the content.
    if any(sig in html_lower for sig in _SPA_FRAMEWORK_SIGNALS):
        return False

    # Check for explicit Cloudflare / bot-challenge pages
    # These have distinctive patterns AND very little real content
    is_cloudflare = "cloudflare" in html_lower and ("ray id" in html_lower or "challenge" in html_lower)
    if is_cloudflare:
        return True

    # For other cases, require both: sparse visible text AND loading keywords
    body_match = re.search(r"<body[^>]*>(.*)</body>", html_lower, re.DOTALL)
    body_text = body_match.group(1) if body_match else html_lower
    visible_text = re.sub(r"<[^>]+>", "", body_text).strip()
    if len(visible_text) < 150:
        if any(p in html_lower for p in _LOADING_SCREEN_PATTERNS):
            return True
    return False


async def _scan_site_metadata(url: str | None, company_name: str | None = None, uen: str | None = None) -> dict:
    if not url:
        return {}

    headers_result = {}
    page_result = {}
    html = ""
    site_accessible = False
    http_status = 0

    try:
        async with get_async_client(timeout=15.0, follow_redirects=True) as client:
            # Use browser-like headers to avoid 403 from WAFs
            resp = await client.get(url, headers=_BROWSER_UA_HEADERS)
            http_status = resp.status_code

            # Retry on 403 with bot UA (some sites prefer identified bots)
            if resp.status_code == 403:
                logger.info(f"Got 403 for {url}, retrying with bot UA")
                resp = await client.get(url, headers={"User-Agent": "BooppaComplianceBot/1.0"})
                http_status = resp.status_code

            # Detect loading/splash screens and retry after delay
            if resp.status_code < 400 and _is_loading_page(resp.text or ""):
                for attempt in range(1, 3):
                    logger.info(
                        f"Loading screen detected for {url}, "
                        f"waiting 30s before retry {attempt}/2"
                    )
                    await asyncio.sleep(30)
                    resp = await client.get(url, headers=_BROWSER_UA_HEADERS)
                    http_status = resp.status_code
                    if not _is_loading_page(resp.text or ""):
                        logger.info(f"Real content received on retry {attempt} for {url}")
                        break
                else:
                    page_result["loading_screen_detected"] = True

            # Determine if we actually got real, scannable content
            if resp.status_code >= 400:
                site_accessible = False
            elif _is_loading_page(resp.text or ""):
                site_accessible = False
            else:
                site_accessible = True

            headers_result = {
                "hsts": bool(resp.headers.get("strict-transport-security")),
                "csp": bool(resp.headers.get("content-security-policy")),
                "x_content_type_options": bool(resp.headers.get("x-content-type-options")),
                "x_frame_options": bool(resp.headers.get("x-frame-options")),
                "referrer_policy": bool(resp.headers.get("referrer-policy")),
                "permissions_policy": bool(resp.headers.get("permissions-policy")),
            }
            html = resp.text or ""
    except Exception as e:
        page_result["scan_error"] = f"metadata_error:{str(e)[:200]}"
        site_accessible = False

    # Record accessibility status — downstream consumers MUST check this
    page_result["site_accessible"] = site_accessible
    page_result["http_status"] = http_status

    if not site_accessible:
        # DO NOT run body checks against inaccessible / empty content.
        # Downstream will see site_accessible=False and produce an
        # "Inaccessible" report instead of false findings.
        if http_status == 403:
            page_result["site_inaccessible_reason"] = (
                "Website returned HTTP 403 Forbidden. Probable causes: "
                "Web Application Firewall (WAF) blocking the scanner IP, "
                "Cloudflare or similar CDN protection, or geo-blocking. "
                "The client should whitelist the Booppa scanner IP or "
                "arrange a scan from a whitelisted network."
            )
        elif http_status >= 400:
            page_result["site_inaccessible_reason"] = (
                f"Website returned HTTP {http_status}. "
                "The site may be down, misconfigured, or blocking automated access."
            )
        elif page_result.get("loading_screen_detected"):
            page_result["site_inaccessible_reason"] = (
                "Website displayed a loading screen or bot-challenge page that did not "
                "resolve after ~1 minute. The actual site content could not be analysed."
            )
        else:
            page_result["site_inaccessible_reason"] = (
                "Could not retrieve analysable content from the website."
            )
        logger.warning(f"Site inaccessible for {url}: {page_result['site_inaccessible_reason']}")
        return {"security_headers": headers_result, **page_result}

    # ── Site is accessible — run body checks ──────────────────────────────────
    html_lower = html.lower()
    combined_html = html_lower

    # Tier 5: detect non-English primary language and fetch an English alternate
    # when one is advertised via <html lang> + <link rel="alternate" hreflang="en">.
    # Singapore sites with Chinese/Malay/Tamil primary content would otherwise
    # false-fail our English keyword checks. The English HTML is appended to
    # combined_html so downstream regex checks (DPO, DNC, NRIC, etc.) see it.
    primary_lang = None
    lang_match = re.search(r"<html[^>]*\blang=[\"']([a-zA-Z-]+)[\"']", html, re.IGNORECASE)
    if lang_match:
        primary_lang = lang_match.group(1).split("-")[0].lower()
    page_result["primary_language"] = primary_lang or "unknown"

    if primary_lang and primary_lang != "en":
        alt_match = re.search(
            r'<link[^>]+rel=[\"\']?alternate[\"\']?[^>]+hreflang=[\"\']?en[\"\']?[^>]+href=[\"\']([^\"\']+)[\"\']',
            html, re.IGNORECASE,
        ) or re.search(
            r'<link[^>]+hreflang=[\"\']?en[\"\']?[^>]+href=[\"\']([^\"\']+)[\"\']',
            html, re.IGNORECASE,
        )
        if alt_match:
            try:
                en_href = alt_match.group(1)
                en_url = en_href if en_href.startswith("http") else urljoin(url, en_href)
                async with get_async_client(timeout=15.0, follow_redirects=True) as client:
                    en_resp = await client.get(en_url, headers=_BROWSER_UA_HEADERS)
                if en_resp.status_code < 400:
                    combined_html += "\n" + (en_resp.text or "").lower()
                    page_result["english_alternate_fetched"] = en_url
            except Exception as e:
                logger.info("English alternate fetch failed for %s: %s", url, e)

    # Privacy policy detection
    privacy_link = None
    match = re.search(r'href=["\"]([^"\"]*privacy[^"\"]*)', html_lower)
    if match:
        privacy_link = match.group(1)
    page_result["privacy_policy"] = {
        "found": bool(privacy_link),
        "link": privacy_link,
    }

    # If privacy policy link is found, fetch it for deeper checks
    policy_html_raw = ""  # original-case policy HTML for the clause classifier
    if privacy_link:
        try:
            privacy_url = (
                privacy_link
                if privacy_link.startswith("http")
                else urljoin(url, privacy_link)
            )
            async with get_async_client(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(
                    privacy_url, headers=_BROWSER_UA_HEADERS
                )
                if resp.status_code < 400:
                    policy_html_raw = resp.text or ""
                    combined_html += "\n" + policy_html_raw.lower()
        except Exception as e:
            page_result["privacy_policy_fetch_error"] = f"privacy_fetch:{str(e)[:200]}"

    # DPO detection
    has_dpo = "data protection officer" in combined_html or re.search(r"\bdpo\b", combined_html)
    dpo_email_match = re.search(r"[\w.+-]+@[^\s\"'>]+", combined_html)
    page_result["dpo_compliance"] = {
        "has_dpo": bool(has_dpo),
        "dpo_email": dpo_email_match.group(0) if dpo_email_match and has_dpo else None,
    }

    # DNC mention detection
    mentions_dnc = "dnc" in combined_html or "do not call" in combined_html or "do-not-call" in combined_html
    page_result["dnc_mention"] = {"mentions_dnc": bool(mentions_dnc)}

    # ── NRIC Exposure (classifier-driven) ────────────────────────────────
    # 1) Harvest candidate snippets from the page HTML (original case for the
    #    LLM, lowercased combined_html only used for cheap pre-screen).
    nric_pre_hit = (
        re.search(r"\bnric\b", combined_html)
        or ("fin number" in combined_html)
        or ("fin no" in combined_html)
        or re.search(r"name=\"[^\"]*(nric|fin)[^\"]*\"", combined_html)
    )
    nric_candidates: list[dict] = []
    pdf_findings: list[dict] = []
    if nric_pre_hit or find_valid_nric_values(html):
        nric_candidates.extend(harvest_candidates(html, source_url=url or ""))
        # 2) Follow linked PDFs (bounded) and harvest snippets from those too.
        try:
            pdf_docs = await scan_linked_pdfs(html, base_url=url or "")
            for doc in pdf_docs:
                pdf_findings.append({"url": doc["url"], "chars": len(doc["text"])})
                nric_candidates.extend(harvest_candidates(doc["text"], source_url=doc["url"]))
        except Exception as e:
            logger.info("PDF NRIC scan failed for %s: %s", url, e)

    # 3) Classify with LLM (falls back to heuristics if no API key).
    nric_provider = DeepSeekProvider(getattr(settings, "DEEPSEEK_API_KEY", None))
    nric_evidences = await classify_candidates(nric_candidates, provider=nric_provider)
    nric_summary = summarise_nric(nric_evidences)

    page_result["nric"] = {
        **nric_summary,
        "pdfs_scanned": pdf_findings,
    }
    # Back-compat fields read by older PDF/score code paths
    page_result["collects_nric"] = nric_summary["kind"] in {"collection", "leakage"}
    if nric_summary["evidence_count"]:
        first = nric_summary["items"][0]
        page_result["nric_evidence"] = (
            f"{first['kind']} — {first['snippet'][:160]} ({first['source_url']})"
        )

    # Cookie banner detection from combined HTML
    cookie_indicators = [
        "cookiebot", "usercentrics", "cookieyes", "onetrust", "osano",
        "iubenda", "cookie-consent", "cookie-banner", "cookie banner",
        "cookie notice", "consentmanager", "data-cookieconsent",
        "booppa-cookie", "booppa_consent",
        "pdpa compliant", "pdpa-compliant",
        "optanon", "evidon", "didomi", "trustarc", "quantcast",
        "accept cookies", "allow cookies", "manage cookies",
        "we use cookies", "this site uses cookies", "cookie preferences",
        "cookie settings", "reject cookies",
    ]
    detected_cookies = [k for k in cookie_indicators if k in combined_html]
    policy_mentions_banner = "cookie banner" in combined_html or "accept all" in combined_html or "reject" in combined_html
    if detected_cookies or policy_mentions_banner:
        page_result["consent_mechanism"] = {
            "has_cookie_banner": True,
            "has_active_consent": True,
            "detected_providers": detected_cookies,
            "policy_mentions_banner": policy_mentions_banner,
        }
    elif "consent_mechanism" not in page_result:
        page_result["consent_mechanism"] = {"has_cookie_banner": False}

    # ── External evidence enrichment (Tier 1) ─────────────────────────────
    # These run in parallel; each enricher has internal error handling and
    # returns a safe default on failure, so we don't need extra try/except.
    enrichment_url = url or ""
    pdpc_task = (
        fetch_pdpc_enforcement(company_name, uen) if company_name or uen
        else asyncio.sleep(0, result={"checked": False, "found": False, "cases": []})
    )
    acra_task = (
        fetch_acra_status(uen, company_name) if company_name or uen
        else asyncio.sleep(0, result={"checked": False, "live": False, "warning": None})
    )
    hosting_task = fetch_hosting_signals(enrichment_url)
    ssl_task = fetch_ssl_grade(enrichment_url)
    try:
        pdpc_result, acra_result, hosting_result, ssl_result = await asyncio.gather(
            pdpc_task, acra_task, hosting_task, ssl_task, return_exceptions=False,
        )
    except Exception as e:
        logger.warning("Evidence enrichment failed for %s: %s", url, e)
        pdpc_result = {"checked": False, "found": False, "cases": []}
        acra_result = {"checked": False, "live": False, "warning": None}
        hosting_result = {"checked": False}
        ssl_result = {"checked": False}

    page_result["pdpc_enforcement"] = pdpc_result
    page_result["acra_live"] = acra_result
    page_result["hosting"] = hosting_result
    page_result["ssl_grade"] = ssl_result

    # ── Privacy policy §13 clause classifier (Tier 2 + multilingual) ─────
    if policy_html_raw:
        try:
            # Dispatch on primary language: English uses the anchor-harvest path,
            # CN/MS/TA go straight to the multilingual LLM classifier.
            _lang = (primary_lang or "en").lower()
            if _lang in {"en", "unknown"} or not _lang:
                clause_snippets = harvest_clause_snippets(policy_html_raw)
                verdicts = await classify_clauses(clause_snippets, provider=nric_provider)
            else:
                verdicts = await classify_clauses_multilingual(
                    policy_html_raw, language=_lang, provider=nric_provider,
                )
            page_result["policy_clauses"] = summarise_policy(verdicts)
        except Exception as e:
            logger.warning("Policy clause classification failed for %s: %s", url, e)
            page_result["policy_clauses"] = {
                "score": 0, "status": "Non-Compliant",
                "present_count": 0, "total": 6, "missing": [], "items": [],
                "error": str(e)[:200],
            }

    return {"security_headers": headers_result, **page_result}


async def _resolve_website_url(raw_url: str | None) -> dict:
    if not raw_url or not isinstance(raw_url, str):
        return {}

    url = raw_url.strip()
    if not url:
        return {}

    # Normalize: strip scheme to get bare host+path
    normalized = url
    for prefix in ("https://", "http://"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix):]
            break

    # Build candidate list — try www. first because bare domains sometimes have
    # higher latency or stricter CDN rules (e.g. Cloudflare proxies www but not apex).
    candidates = []
    if not normalized.startswith("www."):
        candidates.append(f"https://www.{normalized}")
    candidates += [f"https://{normalized}", f"http://{normalized}"]

    # Fallback to root domain if the provided URL has a path (to handle 404s like /SG)
    if "/" in normalized:
        root_domain = normalized.split("/", 1)[0]
        if not root_domain.startswith("www."):
            candidates.append(f"https://www.{root_domain}")
        candidates += [f"https://{root_domain}", f"http://{root_domain}"]

    async with get_async_client(timeout=8.0, follow_redirects=True) as client:
        for candidate in candidates:
            try:
                # Use browser-like headers to avoid 403 from WAFs/CDNs
                resp = await client.get(candidate, headers=_BROWSER_UA_HEADERS)
                # If 403, retry with bot UA (some sites prefer identified bots)
                if resp.status_code == 403:
                    resp = await client.get(
                        candidate,
                        headers={"User-Agent": "BooppaComplianceBot/1.0"},
                    )
                # If it's a 404, we continue to the next candidate (which might be the root domain)
                if resp.status_code == 404 and candidate != candidates[-1]:
                    logger.info(f"URL {candidate} returned 404, trying next candidate")
                    continue
                
                final_url = str(resp.url).rstrip("/")
                return {
                    "resolved_url": final_url,
                    "uses_https": final_url.lower().startswith("https://"),
                    "http_status": resp.status_code,
                }
            except Exception as e:
                logger.warning(f"URL check failed for {candidate}: {e}")

    return {"resolution_error": "all_attempts_failed"}


@celery_app.task(bind=True, max_retries=3, name="process_report_task")
def process_report_task(self, report_id: str):
    """Main report processing task - orchestrates the entire workflow"""
    try:
        # Run async workflow in sync context (use asyncio.run to create a fresh event loop)
        result = asyncio.run(process_report_workflow(report_id))

        logger.info(f"Report {report_id} processed successfully")
        return result

    except Exception as exc:
        logger.error(f"Report processing failed for {report_id}: {exc}")

        # Update report status to failed
        db = SessionLocal()
        try:
            report = ReportRepository.get_by_id(db, str(report_id))
            if report:
                report.status = "failed"
                db.commit()
        finally:
            db.close()

        # Retry with exponential backoff
        countdown = 60 * (2**self.request.retries)  # 1min, 2min, 4min
        raise self.retry(exc=exc, countdown=countdown)


async def process_report_workflow(report_id: str) -> dict:
    """Async workflow for report processing"""
    db = SessionLocal()
    try:
        # Get report from database
        report = ReportRepository.get_by_id(db, str(report_id))
        if not report:
            raise ValueError(f"Report {report_id} not found")

        # Ensure assessment_data is a dict (it might come as a string)
        ad = report.assessment_data or {}
        if not isinstance(ad, dict):
            try:
                ad = json.loads(ad)
            except Exception:
                ad = {}
        
        policy = enforce_tier(ad, report.framework)
        features = policy.get("features", {}) if isinstance(policy, dict) else {}
        
        # Debug logging for tier resolution
        logger.info(f"Tier Resolution for {report_id}: framework={report.framework}, tier={policy.get('tier')}, paid={policy.get('paid')}, pdf_enabled={features.get('pdf')}")
        if isinstance(ad, dict):
            logger.info(f"Payment status for {report_id}: payment_confirmed={ad.get('payment_confirmed')}, product_type={ad.get('product_type')}")
        
        try:
            _set_assessment_values(
                report,
                {
                    "access_checked_at": datetime.now(timezone.utc).isoformat(),
                    "access_allowed": policy.get("allowed"),
                    "access_paid": policy.get("paid"),
                    "access_reason": policy.get("reason"),
                    "tier": policy.get("tier"),
                    "tier_features": features,
                },
            )
            db.commit()
        except Exception:
            db.rollback()

        if not policy.get("allowed"):
            report.status = "blocked"
            report.completed_at = datetime.now(timezone.utc)
            try:
                _set_assessment_values(
                    report,
                    {
                        "access_blocked": True,
                        "access_blocked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

            try:
                dep_updates = log_dependency_event(
                    report.assessment_data,
                    owner_id=str(report.owner_id),
                    report_id=str(report.id),
                    company_name=report.company_name,
                    event_type="access_blocked",
                )
                _set_assessment_values(report, dep_updates)
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "blocked",
                "report_id": report_id,
                "reason": policy.get("reason"),
            }

        # Resolve website URL over the network and store HTTPS status
        try:
            url = None
            if isinstance(report.assessment_data, dict):
                url = report.assessment_data.get("url") or report.company_website
            result = await _resolve_website_url(url)
            if result:
                resolved_url = result.get("resolved_url")
                updates = {}
                if resolved_url:
                    updates["url"] = resolved_url
                    updates["resolved_url"] = resolved_url
                if "uses_https" in result:
                    updates["uses_https"] = bool(result.get("uses_https"))
                if "http_status" in result:
                    updates["http_status"] = result.get("http_status")
                if result.get("resolution_error"):
                    updates["url_resolution_error"] = result.get("resolution_error")
                if updates:
                    _set_assessment_values(report, updates)
                    db.commit()
        except Exception as e:
            logger.warning(
                f"Could not resolve website URL for {report_id}: {e}"
            )

        # Detect cookie banner/consent mechanism from HTML
        try:
            resolved_url = None
            if isinstance(report.assessment_data, dict):
                resolved_url = report.assessment_data.get("resolved_url") or report.assessment_data.get("url")
            cookie_result = await _detect_cookie_banner(resolved_url)
            if cookie_result:
                _set_assessment_values(report, cookie_result)
                db.commit()
        except Exception as e:
            logger.warning(f"Cookie detection failed for {report_id}: {e}")

        # Run broad website metadata scan (privacy policy, DPO, DNC, security headers, NRIC hints)
        try:
            resolved_url = None
            uen = None
            if isinstance(report.assessment_data, dict):
                resolved_url = report.assessment_data.get("resolved_url") or report.assessment_data.get("url")
                uen = report.assessment_data.get("uen")
            metadata_result = await _scan_site_metadata(resolved_url, company_name=report.company_name, uen=uen)
            if metadata_result:
                _set_assessment_values(report, metadata_result)
                db.commit()
        except Exception as e:
            logger.warning(f"Metadata scan failed for {report_id}: {e}")

        # Tier 4: persist per-dimension snapshots for drift detection.
        # Idempotent: if scan data is incomplete we just write fewer rows.
        try:
            _record_dimension_snapshots(db, report)
        except Exception as e:
            logger.warning(f"Dimension snapshot persistence failed for {report_id}: {e}")
            db.rollback()

        # Tier 6: auto-confirm any pending user-marked remediations.
        try:
            _confirm_remediations(db, report)
        except Exception as e:
            logger.warning(f"Remediation auto-confirmation failed for {report_id}: {e}")
            db.rollback()

        # ── Gate: abort findings if site was inaccessible ─────────────────
        # If the scan could not reach the site (403, loading screen, network
        # error), we MUST NOT generate compliance findings — they would be
        # based on empty HTML and therefore false.
        _site_accessible = True
        if isinstance(report.assessment_data, dict):
            _site_accessible = report.assessment_data.get("site_accessible", True)

        if not _site_accessible:
            _reason = ""
            if isinstance(report.assessment_data, dict):
                _reason = report.assessment_data.get("site_inaccessible_reason", "")
                _http_status = report.assessment_data.get("http_status", 0)
            else:
                _http_status = 0
            logger.warning(
                f"Site inaccessible for {report_id} (HTTP {_http_status}). "
                f"Generating inaccessible report instead of findings."
            )
            _set_assessment_values(report, {
                "site_inaccessible": True,
                "site_inaccessible_at": datetime.now(timezone.utc).isoformat(),
            })
            report.status = "site_inaccessible"
            report.ai_narrative = (
                f"SCAN COULD NOT BE COMPLETED — SITE INACCESSIBLE\n\n"
                f"{_reason}\n\n"
                f"HTTP Status Code: {_http_status}\n\n"
                f"No compliance findings have been generated because the scanner "
                f"could not access the target website's content. Producing findings "
                f"without real data would be misleading.\n\n"
                f"RECOMMENDED ACTIONS:\n"
                f"1. Verify the website URL is correct and the site is online.\n"
                f"2. If the site uses a WAF (Cloudflare, Akamai, etc.), whitelist "
                f"the Booppa scanner IP or arrange a scan from a whitelisted network.\n"
                f"3. If the site is geo-restricted, arrange a scan from within the "
                f"allowed region (Singapore).\n"
                f"4. Once access is confirmed, request a rescan."
            )
            report.completed_at = datetime.now(timezone.utc)
            db.commit()

            # Still generate a PDF if paid, but it will be an "inaccessible" report
            if features.get("pdf") and bool(policy.get("paid")):
                try:
                    _url = ""
                    if isinstance(report.assessment_data, dict):
                        _url = (report.assessment_data.get("resolved_url")
                                or report.assessment_data.get("url")
                                or report.company_website or "")
                    pdf_service = PDFService()
                    pdf_data = {
                        "report_id": str(report.id),
                        "framework": report.framework,
                        "company_name": report.company_name,
                        "created_at": report.created_at.isoformat(),
                        "status": "site_inaccessible",
                        "website_url": _url,
                        "ai_narrative": report.ai_narrative,
                        "structured_report": {
                            "executive_summary": report.ai_narrative,
                            "detailed_findings": [],
                            "recommendations": [],
                            "legal_references": [],
                        },
                        "site_inaccessible": True,
                        "site_inaccessible_reason": _reason,
                        "http_status": _http_status,
                        "payment_confirmed": bool(policy.get("paid")),
                        "tx_hash": None,
                        "audit_hash": None,
                        "contact_email": (
                            report.assessment_data.get("contact_email")
                            if isinstance(report.assessment_data, dict)
                            else None
                        ),
                        "base_url": "https://www.booppa.io",
                    }
                    pdf_bytes = pdf_service.generate_pdf(pdf_data)
                    storage = S3Service()
                    s3_url = await storage.upload_pdf(pdf_bytes, str(report.id))
                    report.s3_url = s3_url
                    report.file_key = f"reports/{report.id}.pdf"
                    db.commit()
                except Exception as e:
                    logger.error(f"Inaccessible-report PDF failed for {report_id}: {e}")

            return {
                "status": "site_inaccessible",
                "report_id": report_id,
                "reason": _reason,
                "http_status": _http_status,
            }

        # Step 1: Generate structured AI report (full for paid tiers, light for free)
        logger.info(f"Step 1: Generating AI report for {report_id}")
        structured_report = None
        narrative = ""

        if features.get("ai_full"):
            # ── Remediation Tracking ─────────────────────────────────────────
            # Check for previous reports to find resolved violations
            remediations = []
            try:
                # Find the latest completed report for this same website
                previous_report = (
                    db.query(Report)
                    .filter(Report.company_website == report.company_website)
                    .filter(Report.id != report.id)
                    .filter(Report.status == "completed")
                    .order_by(Report.created_at.desc())
                    .first()
                )
                
                if previous_report and isinstance(previous_report.assessment_data, dict):
                    prev_structured = previous_report.assessment_data.get("booppa_report", {})
                    prev_findings = prev_structured.get("detailed_findings", [])
                    
                    # Get current raw scan findings to see what is "fixed"
                    # Note: We compare against raw detection before AI narrative is built
                    cookie_check = report.assessment_data.get("consent_mechanism", {})
                    has_cookie_banner = cookie_check.get("has_cookie_banner", False)
                    
                    for f in prev_findings:
                        f_type = f.get("type", "").lower()
                        # If previously had cookie violation but now has banner
                        if "cookie" in f_type and has_cookie_banner:
                            remediations.append({
                                "type": "COOKIE_CONSENT_IMPLEMENTATION",
                                "description": "Compliant cookie consent banner detected and verified.",
                                "resolved_at": datetime.now(timezone.utc).isoformat(),
                                "previous_report_id": str(previous_report.id),
                                "evidence_hash": hashlib.sha256(f"cookie-remediation-{report.id}".encode()).hexdigest()
                            })
            except Exception as e:
                logger.warning(f"Remediation tracking failed for {report_id}: {e}")

            booppa_ai = BooppaAIService()
            
            # Anchor remediations on blockchain if any
            if remediations and features.get("blockchain"):
                blockchain_svc = BlockchainService()
                for rem in remediations:
                    try:
                        meta = f"Booppa Proof: {rem['description']} for {report.company_website}"
                        tx_hash = await blockchain_svc.anchor_evidence(rem["evidence_hash"], meta)
                        if tx_hash:
                            rem["tx_hash"] = tx_hash
                            rem["anchored"] = True
                    except Exception as e:
                        logger.error(f"Failed to anchor remediation {rem['type']}: {e}")

            scan_data = {**report.assessment_data, "company_name": report.company_name}
            structured_report = await booppa_ai.generate_compliance_report(scan_data)
            
            if structured_report and remediations:
                structured_report["remediation_history"] = remediations
            # Keep a human-readable narrative for legacy fields
            try:
                ai_service = AIService()
                narrative = ai_service._format_report_as_narrative(structured_report)
            except Exception:
                narrative = structured_report.get("executive_summary") or ""

            report.ai_model_used = structured_report.get("report_metadata", {}).get(
                "ai_model", "Booppa"
            )
            # persist structured report into assessment_data for traceability
            try:
                _set_assessment_values(
                    report,
                    {
                        "booppa_report": structured_report,
                        "booppa_report_saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                logger.warning(
                    "Could not attach structured report into assessment_data"
                )
        else:
            url_value = None
            if isinstance(report.assessment_data, dict):
                url_value = report.assessment_data.get("url")
            light_payload = {
                "company_name": report.company_name,
                "url": url_value or report.company_website,
                "scan_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "detected_laws": (
                    report.assessment_data.get("detected_laws", [])
                    if isinstance(report.assessment_data, dict)
                    else []
                ),
                "overall_risk_score": (
                    report.assessment_data.get("overall_risk_score")
                    if isinstance(report.assessment_data, dict)
                    else 0
                ),
                "uses_https": (
                    report.assessment_data.get("uses_https", True)
                    if isinstance(report.assessment_data, dict)
                    else True
                ),
            }
            light_report = await ai_preview(light_payload)
            narrative = light_report.get("summary") or light_report.get(
                "recommendation", ""
            )
            report.ai_model_used = "Booppa Light"
            try:
                _set_assessment_values(
                    report,
                    {
                        "light_ai_report": light_report,
                        "light_ai_saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                logger.warning("Could not attach light AI output into assessment_data")

        report.ai_narrative = narrative
        db.commit()

        # Step 2: Compute evidence hash
        logger.info(f"Step 2: Computing evidence hash for {report_id}")
        evidence_data = {
            "report_id": str(report.id),
            "framework": report.framework,
            "company": report.company_name,
            "assessment_data": report.assessment_data,
            "ai_narrative": narrative,
            "timestamp": report.created_at.isoformat(),
        }

        evidence_json = json.dumps(evidence_data, sort_keys=True)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        report.audit_hash = evidence_hash
        db.commit()

        try:
            append_audit_event(
                db,
                report_id=str(report.id),
                action="report_hash_created",
                actor=str(report.owner_id),
                hash_value=evidence_hash,
                metadata={"framework": report.framework},
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Failed to append audit chain for {report_id}: {e}")

        # Step 3: Anchor on blockchain (only if payment confirmed)
        logger.info(f"Step 3: Anchoring evidence on blockchain for {report_id}")
        payment_confirmed = bool(policy.get("paid"))

        tx_hash = None
        if features.get("blockchain") and payment_confirmed:
            blockchain = BlockchainService()
            metadata = f"report:{report.id}"
            tx_hash = await blockchain.anchor_evidence(evidence_hash, metadata=metadata)
            report.tx_hash = tx_hash
            db.commit()
        else:
            # leave tx_hash None; PDF will point to pending verification
            report.tx_hash = None
            db.commit()

        verify_base = settings.VERIFY_BASE_URL.rstrip("/")
        verify_url = f"{verify_base}/verify/{evidence_hash}"

        if features.get("pdf") and payment_confirmed:
            try:
                _set_assessment_values(
                    report,
                    {
                        "verify_url": verify_url,
                        "proof_header": "BOOPPA-PROOF-SG",
                        "schema_version": "1.0",
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

        try:
            if features.get("pdf") and payment_confirmed:
                verify_updates = register_verification(
                    report.assessment_data,
                    evidence_hash=evidence_hash,
                    tx_hash=tx_hash,
                )
                _set_assessment_values(report, verify_updates)
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to register verification for {report_id}: {e}")

        # Ensure a site screenshot is present for on-page report (even if PDF is skipped).
        try:
            existing_screenshot = None
            if isinstance(report.assessment_data, dict):
                existing_screenshot = report.assessment_data.get("site_screenshot")
            if not existing_screenshot:
                url = None
                if isinstance(report.assessment_data, dict):
                    url = report.assessment_data.get("url") or report.company_website
                if isinstance(url, str) and url and not url.lower().startswith(("http://", "https://")):
                    url = f"https://{url}"
                if url:
                    ss_b64 = await _capture_screenshot_with_timeout(url, timeout=25)
                    if ss_b64:
                        try:
                            _set_assessment_values(report, {"site_screenshot": ss_b64})
                            db.commit()
                        except Exception as e:
                            logger.warning(
                                f"Could not store site screenshot for {report_id}: {e}"
                            )
                    else:
                        thum_b64, thum_err = await _fetch_thum_io_base64(url)
                        if thum_b64:
                            try:
                                _set_assessment_values(report, {"site_screenshot": thum_b64})
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store thum.io screenshot for {report_id}: {e}"
                                )
                        else:
                            try:
                                _set_assessment_values(
                                    report,
                                    {
                                        "screenshot_error": thum_err
                                        or "capture_failed_or_timeout",
                                        "screenshot_url": url,
                                    },
                                )
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store screenshot error for {report_id}: {e}"
                                )
        except Exception as e:
            try:
                _set_assessment_values(
                    report,
                    {
                        "screenshot_error": f"exception:{str(e)[:200]}",
                        "screenshot_url": report.assessment_data.get("url")
                        if isinstance(report.assessment_data, dict)
                        else report.company_website,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
            logger.warning(
                f"Could not capture site screenshot for {report_id}: {e}"
            )

        # If this report is meant for on-page only, mark as completed and skip PDF generation.
        try:
            on_page_only = False
            if isinstance(report.assessment_data, dict):
                on_page_only = bool(report.assessment_data.get("on_page_only"))
            if not features.get("pdf"):
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": False,
                        "pdf_reason": "tier_restriction",
                    },
                )
                report.status = "completed"
                report.completed_at = datetime.now(timezone.utc)
                db.commit()

                # Send notification email without PDF link
                email_service = EmailService()
                try:
                    to_email = None
                    if isinstance(report.assessment_data, dict):
                        to_email = report.assessment_data.get(
                            "contact_email"
                        ) or report.assessment_data.get("customer_email")
                    if to_email:
                        await email_service.send_report_ready_email(
                            to_email=to_email,
                            report_url=None,
                            user_name=(report.company_name or "User"),
                            report_id=str(report.id),
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to send notification email for {report_id}: {e}"
                    )

                try:
                    dep_updates = log_dependency_event(
                        report.assessment_data,
                        owner_id=str(report.owner_id),
                        report_id=str(report.id),
                        company_name=report.company_name,
                        event_type="report_completed",
                        extra={"delivery": "no_pdf"},
                    )
                    _set_assessment_values(report, dep_updates)
                    db.commit()
                except Exception:
                    db.rollback()

                return {
                    "status": "completed",
                    "report_id": report_id,
                    "pdf_url": None,
                    "tx_hash": tx_hash,
                }
            if on_page_only:
                _set_assessment_values(report, {"on_page_ready": True})
                report.status = "completed"
                report.completed_at = datetime.now(timezone.utc)
                db.commit()

                try:
                    dep_updates = log_dependency_event(
                        report.assessment_data,
                        owner_id=str(report.owner_id),
                        report_id=str(report.id),
                        company_name=report.company_name,
                        event_type="report_completed",
                        extra={"delivery": "on_page"},
                    )
                    _set_assessment_values(report, dep_updates)
                    db.commit()
                except Exception:
                    db.rollback()

                return {
                    "status": "completed",
                    "report_id": report_id,
                    "pdf_url": None,
                    "tx_hash": tx_hash,
                }
        except Exception as e:
            logger.warning(f"Failed to finalize on-page report {report_id}: {e}")

        # Optional: skip PDF generation and S3 upload
        if settings.SKIP_PDF_GENERATION:
            logger.info(f"Skipping PDF generation for {report_id}")
            report.s3_url = None
            report.file_key = None
            report.status = "completed"
            report.completed_at = datetime.now(timezone.utc)
            try:
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": False,
                        "s3_uploaded": False,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

            # Send notification email without PDF link
            email_service = EmailService()
            try:
                to_email = None
                if isinstance(report.assessment_data, dict):
                    to_email = report.assessment_data.get("contact_email") or report.assessment_data.get(
                        "customer_email"
                    )
                if to_email:
                    await email_service.send_report_ready_email(
                        to_email=to_email,
                        report_url=None,
                        user_name=(report.company_name or "User"),
                        report_id=str(report.id),
                    )
            except Exception as e:
                logger.error(
                    f"Failed to send notification email for {report_id}: {e}"
                )

            try:
                dep_updates = log_dependency_event(
                    report.assessment_data,
                    owner_id=str(report.owner_id),
                    report_id=str(report.id),
                    company_name=report.company_name,
                    event_type="report_completed",
                    extra={"delivery": "no_pdf"},
                )
                _set_assessment_values(report, dep_updates)
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "completed",
                "report_id": report_id,
                "pdf_url": None,
                "tx_hash": tx_hash,
            }

        # Step 4: Generate PDF with QR code
        logger.info(f"Step 4: Generating PDF for {report_id}")
        pdf_service = PDFService()

        pdf_data = {
            "report_id": str(report.id),
            "framework": report.framework,
            "company_name": report.company_name,
            "created_at": report.created_at.isoformat(),
            "status": "completed",
            "tx_hash": tx_hash,
            "audit_hash": evidence_hash,
            "ai_narrative": narrative,
            "structured_report": structured_report,
            "payment_confirmed": payment_confirmed,
            "tier": policy.get("tier"),
            "proof_header": (
                report.assessment_data.get("proof_header")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or ("BOOPPA-PROOF-SG" if payment_confirmed else None),
            "schema_version": (
                report.assessment_data.get("schema_version")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or ("1.0" if payment_confirmed else None),
            "verify_url": (
                report.assessment_data.get("verify_url")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or (verify_url if payment_confirmed else None),
            "contact_email": (
                report.assessment_data.get("contact_email")
                if isinstance(report.assessment_data, dict)
                else None
            ),
            "base_url": (
                report.assessment_data.get("base_url")
                if isinstance(report.assessment_data, dict)
                and report.assessment_data.get("base_url")
                else "https://www.booppa.io"
            ),
            "website_url": (
                (report.assessment_data.get("resolved_url")
                 or report.assessment_data.get("url"))
                if isinstance(report.assessment_data, dict)
                else None
            ) or report.company_website,
        }

        # Pass raw scan evidence so PDF scores are computed from actual data
        if isinstance(report.assessment_data, dict):
            for _scan_key in (
                "security_headers", "consent_mechanism", "privacy_policy",
                "dpo_compliance", "dnc_mention", "nric_evidence",
                "http_status", "site_accessible",
                # Tier 1-5 keys consumed by the upgraded score table:
                "nric", "policy_clauses", "pdpc_enforcement", "hosting",
                "trackers", "ssl_grade", "primary_language",
            ):
                if _scan_key in report.assessment_data:
                    pdf_data[_scan_key] = report.assessment_data[_scan_key]

        # Tier 6: attach this user's remediation history so the PDF can show
        # confirmed fixes and pending items. Best-effort — empty list on error.
        try:
            from app.core.models import FindingRemediation
            from app.services.finding_keys import label_for_key
            rem_rows = (
                db.query(FindingRemediation)
                .filter(FindingRemediation.vendor_id == report.owner_id)
                .order_by(FindingRemediation.marked_at.desc())
                .limit(20)
                .all()
            )
            pdf_data["remediations"] = [
                {
                    "finding_key": r.finding_key,
                    "label": label_for_key(r.finding_key),
                    "status": r.status,
                    "confirmation_status": r.confirmation_status,
                    "marked_at": r.marked_at.isoformat() if r.marked_at else None,
                    "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
                }
                for r in rem_rows
            ]
        except Exception as e:
            logger.warning(f"Remediation history load failed for {report_id}: {e}")
            pdf_data["remediations"] = []

        # Ensure a site screenshot is present for every PDF. Prefer existing data, otherwise capture.
        if not pdf_data.get("site_screenshot"):
            try:
                url = None
                if isinstance(report.assessment_data, dict):
                    url = report.assessment_data.get("url") or report.company_website
                if url:
                    ss_b64 = await _capture_screenshot_with_timeout(url, timeout=25)
                    if ss_b64:
                        pdf_data["site_screenshot"] = ss_b64
                        try:
                            _set_assessment_values(report, {"site_screenshot": ss_b64})
                            db.commit()
                        except Exception as e:
                            logger.warning(
                                f"Could not store site screenshot for {report_id}: {e}"
                            )
                    else:
                        thum_b64, thum_err = await _fetch_thum_io_base64(url)
                        if thum_b64:
                            pdf_data["site_screenshot"] = thum_b64
                            try:
                                _set_assessment_values(report, {"site_screenshot": thum_b64})
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store thum.io screenshot for {report_id}: {e}"
                                )
                        else:
                            try:
                                _set_assessment_values(
                                    report,
                                    {
                                        "screenshot_error": thum_err
                                        or "capture_failed_or_timeout",
                                        "screenshot_url": url,
                                    },
                                )
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store screenshot error for {report_id}: {e}"
                                )
            except Exception as e:
                try:
                    _set_assessment_values(
                        report,
                        {
                            "screenshot_error": f"exception:{str(e)[:200]}",
                            "screenshot_url": report.assessment_data.get("url")
                            if isinstance(report.assessment_data, dict)
                            else report.company_website,
                        },
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                logger.warning(
                    f"Could not capture site screenshot for {report_id}: {e}"
                )

        try:
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
            logger.info(
                f"PDF generated for {report_id} ({len(pdf_bytes)} bytes)"
            )
            try:
                _persist_vals = {
                    "pdf_generated": True,
                    "pdf_generated_at": datetime.now(timezone.utc).isoformat(),
                }
                # C2 single-source-of-truth: persist the EXACT dimension-weighted
                # compliance score this PDF printed (stashed onto pdf_data by
                # _compliance_score_table) + the URL it displayed. This is the
                # canonical scan report — the Compliance Evidence Cover Sheet and
                # the activation email read these verbatim instead of recomputing
                # a divergent 100-risk number (the 53-vs-54 forensic-audit bug).
                # Previously this persist lived ONLY in _fulfill_pdpa, so reports
                # generated by this main scan path carried no compliance_score and
                # the cover sheet silently fell back to 100-risk.
                _computed = pdf_data.get("computed_overall_compliance_score")
                if _computed is None and isinstance(pdf_data.get("scan_data"), dict):
                    _computed = pdf_data["scan_data"].get("computed_overall_compliance_score")
                if _computed is not None:
                    _persist_vals["compliance_score"] = _computed
                _disp_url = pdf_data.get("website_url")
                if _disp_url:
                    _persist_vals["display_url"] = _disp_url
                _set_assessment_values(report, _persist_vals)
                db.commit()
            except Exception:
                db.rollback()
        except Exception as e:
            logger.error(f"PDF generation failed for {report_id}: {e}")
            raise

        # Step 5: Upload to S3 with retry/backoff
        logger.info(f"Step 5: Uploading PDF to S3 for {report_id}")
        storage = S3Service()
        max_attempts = 3
        pdf_url = None
        for attempt in range(1, max_attempts + 1):
            try:
                pdf_url = await storage.upload_pdf(pdf_bytes, str(report.id))
                report.s3_url = pdf_url
                report.file_key = f"reports/{report.id}.pdf"
                try:
                    _set_assessment_values(
                        report,
                        {
                            "s3_uploaded": True,
                            "s3_uploaded_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception:
                    logger.warning(f"Failed to mark S3 upload status for {report_id}")
                # Mark as completed once upload succeeds so frontend can access URL
                report.status = "completed"
                report.completed_at = datetime.now(timezone.utc)
                db.commit()
                break
            except Exception as e:
                logger.error(f"S3 upload attempt {attempt} failed for {report_id}: {e}")
                if attempt == max_attempts:
                    # propagate so workflow marks failed and triggers retry
                    raise
                await asyncio.sleep(min(10, 2**attempt))

        # Step 6: Send notification email (non-fatal)
        logger.info(f"Step 6: Sending notification for {report_id}")
        email_service = EmailService()
        try:
            to_email = None
            if isinstance(report.assessment_data, dict):
                to_email = report.assessment_data.get("contact_email") or report.assessment_data.get(
                    "customer_email"
                )
            if not to_email:
                raise ValueError("Missing contact email for report notification")

            await email_service.send_report_ready_email(
                to_email=to_email,
                report_url=pdf_url,
                user_name=(report.company_name or "User"),
                report_id=str(report.id),
            )
        except Exception as e:
            logger.error(f"Failed to send notification email for {report_id}: {e}")

        # If not already marked completed (defensive), set completion timestamp
        try:
            if report.status != "completed":
                report.status = "completed"
                report.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            db.rollback()

        try:
            dep_updates = log_dependency_event(
                report.assessment_data,
                owner_id=str(report.owner_id),
                report_id=str(report.id),
                company_name=report.company_name,
                event_type="report_completed",
                extra={"delivery": "pdf" if pdf_url else "no_pdf"},
            )
            _set_assessment_values(report, dep_updates)
            db.commit()
        except Exception:
            db.rollback()

        return {
            "status": "completed",
            "report_id": report_id,
            "pdf_url": pdf_url,
            "tx_hash": tx_hash,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Report workflow failed: {e}")
        try:
            report = ReportRepository.get_by_id(db, str(report_id))
            if report:
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["last_processing_error"] = str(e)[:500]
                assessment["last_processing_error_at"] = datetime.now(timezone.utc).isoformat()
                report.assessment_data = assessment
                report.status = "failed"
                db.commit()
        except Exception as inner_exc:
            logger.error(
                f"Failed to persist processing error for {report_id}: {inner_exc}"
            )
        raise
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, name="activate_subscription_task")
def activate_subscription_task(
    self,
    product_type: str,
    customer_email: str | None,
    stripe_subscription_id: str | None,
    stripe_customer_id: str | None,
):
    """Celery task: persist subscription state after a successful Stripe checkout or renewal."""
    try:
        from app.services.fulfillment import activate_subscription
        asyncio.run(activate_subscription(
            product_type=product_type,
            customer_email=customer_email,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=stripe_customer_id,
        ))
        logger.info(f"[activate_subscription_task] plan={product_type} email={customer_email}")
    except Exception as exc:
        logger.error(f"[activate_subscription_task] failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=3, name="fulfill_bundle_task")
def fulfill_bundle_task(
    self,
    product_type: str,
    report_id: str | None,
    customer_email: str | None,
    metadata: dict,
    session_id: str | None,
):
    """Celery task: fan out bundle fulfillment to individual component Celery tasks."""
    try:
        from app.services.fulfillment import fulfill_bundle
        asyncio.run(fulfill_bundle(
            product_type=product_type,
            report_id=report_id,
            customer_email=customer_email,
            metadata=metadata,
            session_id=session_id,
        ))
        logger.info(f"[fulfill_bundle_task] bundle={product_type} email={customer_email}")
    except Exception as exc:
        logger.error(f"[fulfill_bundle_task] failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=2, name="fire_strategy_6_task")
def fire_strategy_6_task(self, sector: str | None, rfp_title: str):
    """Celery task: notify top-5 verified sector vendors about a new procurement opportunity."""
    try:
        from app.services.fulfillment import fire_strategy_6
        asyncio.run(fire_strategy_6(sector=sector, buyer_rfp_title=rfp_title))
        logger.info(f"[fire_strategy_6_task] sector={sector}")
    except Exception as exc:
        logger.warning(f"[fire_strategy_6_task] failed (attempt {self.request.retries + 1}): {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=2, name="send_referral_reward_email_task")
def send_referral_reward_email_task(self, referrer_email: str):
    """Celery task: send referral conversion reward notification email."""
    from app.services.email_layout import branded_email_html, email_button
    body_html = branded_email_html(
        "<h2 style='margin:0 0 12px;font-size:20px;color:#0f172a;'>Your referral paid off!</h2>"
        "<p style='margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;'>A vendor you "
        "referred just made their first purchase. 30 free days have been added to your account.</p>"
        + email_button("https://www.booppa.io/vendor/dashboard", "View dashboard"),
        title="Your referral converted",
        preheader="A vendor you referred purchased — 30 free days added.",
    )
    try:
        asyncio.run(EmailService().send_html_email(
            to_email=referrer_email,
            subject="Your referral converted — free month added",
            body_html=body_html,
        ))
        logger.info(f"[send_referral_reward_email_task] sent to {referrer_email}")
    except Exception as exc:
        logger.warning(f"[send_referral_reward_email_task] failed: {exc}")
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(bind=True, max_retries=3, name="fulfill_vendor_proof_task")
def fulfill_vendor_proof_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: create VerifyRecord, set compliance baseline, send badge email."""
    try:
        from app.services.fulfillment import fulfill_vendor_proof
        asyncio.run(fulfill_vendor_proof(report_id=report_id, customer_email=customer_email))
        logger.info(f"Vendor proof fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"Vendor proof fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=10, name="fulfill_pdpa_task")
def fulfill_pdpa_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: generate PDPA PDF, update compliance score, write CertificateLog, send email.

    Fulfillment is chained to the scan (see `_fulfill_pdpa`), so this retry path is
    only a fallback. Backoff is capped at 10 min so a fallback retry can't push the
    confirmation email hours out.
    """
    try:
        from app.services.fulfillment import fulfill_pdpa
        asyncio.run(fulfill_pdpa(report_id=report_id, customer_email=customer_email, raise_if_incomplete=True))
        logger.info(f"PDPA snapshot fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"PDPA fulfillment failed for {report_id}: {exc}")
        countdown = min(60 * (2 ** self.request.retries), 600)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_notarization_task")
def fulfill_notarization_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: anchor, generate PDF, and deliver notarization certificate."""
    try:
        from app.services.fulfillment import fulfill_notarization
        asyncio.run(fulfill_notarization(report_id=report_id, customer_email=customer_email))
        logger.info(f"Notarization fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"Notarization fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_rfp_task")
def fulfill_rfp_task(
    self,
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
    session_id: str | None = None,
    intake_data: dict | None = None,
    allow_incomplete: bool = False,
):
    """Celery task: generate and deliver the RFP Kit evidence package."""
    try:
        from app.services.fulfillment import fulfill_rfp_package
        asyncio.run(fulfill_rfp_package(
            product_type=product_type,
            vendor_id=vendor_id,
            vendor_email=vendor_email,
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_description=rfp_description,
            session_id=session_id,
            intake_data=intake_data,
            allow_incomplete=allow_incomplete,
        ))
        logger.info(f"RFP package fulfilled for vendor {vendor_id} session {session_id}")
    except Exception as exc:
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {exc}")
        try:
            from celery.exceptions import MaxRetriesExceededError
            countdown = 60 * (2 ** self.request.retries)
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            logger.error(f"RFP fulfillment permanently failed for vendor {vendor_id} after {self.max_retries} retries")
            if session_id:
                from app.core.cache import cache as cache_mod
                cache_mod.set(
                    cache_mod.cache_key(f"rfp_result:{session_id}"),
                    {"error": True, "detail": "Generation failed. Please contact support."},
                    ttl=86400
                )
            raise


@celery_app.task(bind=True, max_retries=5, name="anchor_signed_cover_sheet_task")
def anchor_signed_cover_sheet_task(self, report_id: str, customer_email: str | None = None, company_name: str = ""):
    """
    Anchor the signed Cover Sheet PDF on-chain, then re-fire the cover sheet
    fulfillment task to regenerate the PDF (Section 5 now shows the signed tx)
    and email the user a final blockchain receipt.

    Uses force=True on anchor_evidence so a fresh tx_hash is always returned
    even when the hash happens to already be on-chain. The previous
    implementation silently exited on the duplicate-skip path, leaving the
    buyer's UI stuck on "Anchoring on-chain..." forever. If anchoring still
    fails after max_retries, we persist anchor_failed=True on the Report so
    the frontend can surface a clear error + retry CTA instead of a
    perpetual spinner.
    """
    try:
        from app.core.models import Report
        db = SessionLocal()
        try:
            report = ReportRepository.get_by_id(db, str(report_id))
            if not report:
                logger.warning(f"[SignedCS] Report {report_id} not found, skipping anchor")
                return
            ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
            file_hash = ad.get("file_hash") or report.audit_hash
            if not file_hash:
                logger.error(f"[SignedCS] Report {report_id} has no file_hash, cannot anchor")
                return
            blockchain = BlockchainService()
            tx = asyncio.run(blockchain.anchor_evidence(
                file_hash,
                metadata=f"signed_cover_sheet:{report_id}",
                force=True,
            ))
            if tx:
                from sqlalchemy.orm.attributes import flag_modified
                report.tx_hash = tx
                report.completed_at = datetime.now(timezone.utc)
                ad["blockchain_anchored_at"] = report.completed_at.isoformat()
                # Clear any prior failure flag (this might be a recovery retry).
                ad.pop("anchor_failed", None)
                ad.pop("anchor_failed_at", None)
                ad.pop("anchor_failed_reason", None)
                ad.pop("anchor_sweep_attempts", None)
                report.assessment_data = ad
                # assessment_data is a plain JSON column (no MutableDict), so an
                # in-place mutation isn't auto-detected — flag it explicitly or
                # the cleared failure flags silently won't persist.
                flag_modified(report, "assessment_data")
                db.commit()
                logger.info(f"[SignedCS] Anchored {report_id} tx={tx[:12]}…")
            else:
                # tx is falsy even with force=True — RPC dropped the call,
                # contract reverted silently, or some other transient issue.
                # Raise so Celery retries; the persisted failure flag only
                # gets set in the max-retries-exhausted path below.
                raise RuntimeError(
                    f"[SignedCS] anchor_evidence returned no tx_hash for {report_id} "
                    f"(file_hash={file_hash[:12]}…). Will retry."
                )
        finally:
            db.close()

        # Regenerate cover sheet so Section 5 picks up the signed tx, and send final receipt.
        fulfill_cover_sheet_task.apply_async(
            kwargs={
                "bundle_type": "compliance_evidence_pack",
                "customer_email": customer_email,
                "company_name": company_name,
                "metadata": {"force": True, "regen_signed": True},
            },
            countdown=15,
        )
    except Exception as exc:
        logger.error(f"[SignedCS] Anchor failed for {report_id}: {exc}")
        # Retry with exponential backoff. After max_retries, mark the Report
        # so the frontend can stop spinning and surface a recovery CTA.
        try:
            countdown = 60 * (2 ** self.request.retries)
            raise self.retry(exc=exc, countdown=countdown)
        except Exception:
            # Max retries exhausted (or self.retry raised something else).
            # Mark the Report so the UI knows to stop spinning.
            try:
                from app.core.models import Report
                _db = SessionLocal()
                try:
                    _r = ReportRepository.get_by_id(db, str(report_id))
                    if _r:
                        from sqlalchemy.orm.attributes import flag_modified
                        _ad = _r.assessment_data if isinstance(_r.assessment_data, dict) else {}
                        _ad["anchor_failed"] = True
                        _ad["anchor_failed_at"] = datetime.now(timezone.utc).isoformat()
                        _ad["anchor_failed_reason"] = str(exc)[:300]
                        _r.assessment_data = _ad
                        # Plain JSON column — without flag_modified this in-place
                        # mutation never persists, so the status endpoint would
                        # keep showing the buyer a perpetual "Anchoring…" spinner
                        # and the retry sweep would never see anchor_failed=True.
                        flag_modified(_r, "assessment_data")
                        _db.commit()
                        logger.error(
                            f"[SignedCS] Marked {report_id} anchor_failed=True after max retries"
                        )
                finally:
                    _db.close()
            except Exception as flag_err:
                logger.error(f"[SignedCS] Could not persist anchor_failed: {flag_err}")
            # Re-raise so Celery records the task as failed.
            raise


def _build_compliance_bundle_zip(db, user_id, company_name, cover_pdf_bytes):
    """Single evidence archive for a completed Compliance Bundle.

    Bundles a one-page cover letter + the signed Cover Sheet + the cycle
    documents (PDPA Snapshot, RFP Complete Kit, ROPA) into one ZIP the buyer can
    hand to an enterprise/procurement/PDPC reviewer. Best-effort: each document
    is fetched from S3 by its stored key; any miss is skipped, never fatal.
    Returns (filename, zip_bytes) or None if nothing could be assembled.
    """
    import zipfile
    from io import BytesIO as _BytesIO

    from app.core.models import Report
    from app.services.storage import S3Service

    FRAMEWORK_FILES = {
        "pdpa_quick_scan": "PDPA_Snapshot",
        "rfp_complete": "RFP_Complete_Kit",
        "ropa_lite": "ROPA",
        "compliance_evidence_signed_sheet": "Cover_Sheet_Signed",
    }
    safe_co = (company_name or "Company").replace("/", "-").replace(" ", "-")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    members: list[tuple[str, bytes]] = []

    # 1) Cover letter — what the bundle contains + which PDPC levels it covers.
    try:
        from io import BytesIO as _B
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import inch
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        _buf = _B()
        _doc = SimpleDocTemplate(_buf, pagesize=A4, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                                 topMargin=0.8 * inch, bottomMargin=0.8 * inch)
        _st = get_unified_styles()
        _doc.build([
            Paragraph(f"Compliance Evidence Bundle — {company_name or 'Your Organisation'}", _st["Title"]),
            Paragraph(f"Generated {date_str} · Booppa", _st["Normal"]),
            Spacer(1, 16),
            Paragraph(
                "This archive contains your blockchain-anchored compliance evidence: the PDPA "
                "Snapshot, RFP Complete Kit, Record of Processing Activities (ROPA), and the "
                "signed Cover Sheet that indexes them with their on-chain anchors.", _st["BodyText"]),
            Spacer(1, 10),
            Paragraph(
                "<b>Coverage:</b> PDPC Compliance Levels 1 and 2 — automated website evidence "
                "(Level 1) and documented data-processing activities / ROPA (Level 2). For "
                "Levels 3–6, see the Compliance Evidence Pack.", _st["BodyText"]),
        ])
        members.append((f"00_Cover_Letter_{safe_co}.pdf", _buf.getvalue()))
    except Exception as _cl_err:
        logger.warning("[BundleZip] cover letter render failed: %s", _cl_err)

    # 2) The cover sheet we just generated (bytes already in hand).
    if cover_pdf_bytes:
        members.append((f"Cover_Sheet_{safe_co}_{date_str}.pdf", cover_pdf_bytes))

    # 3) Cycle documents — fetch each most-recent anchored Report from S3.
    try:
        s3 = S3Service()
        rows = (
            db.query(Report)
            .filter(
                Report.owner_id == user_id,
                Report.framework.in_(list(FRAMEWORK_FILES.keys())),
                Report.tx_hash.isnot(None),
            )
            .order_by(Report.created_at.desc())
            .all()
        )
        seen: set[str] = set()
        for r in rows:
            if r.framework in seen:
                continue
            key = r.file_key
            if not key and isinstance(r.assessment_data, dict):
                key = r.assessment_data.get("s3_key")
            if not key:
                continue
            try:
                data = s3.s3_client.get_object(Bucket=s3.bucket, Key=key)["Body"].read()
            except Exception as _ferr:
                logger.warning("[BundleZip] fetch failed for %s (%s): %s", r.framework, key, _ferr)
                continue
            seen.add(r.framework)
            members.append((f"{FRAMEWORK_FILES[r.framework]}_{safe_co}_{date_str}.pdf", data))
    except Exception as _docs_err:
        logger.warning("[BundleZip] cycle-document collection failed: %s", _docs_err)

    # Only worth sending once at least one cycle document is in the archive
    # (cover letter + cover sheet alone aren't a "bundle").
    cycle_added = {m[0].split("_" + safe_co)[0] for m in members} & {v for v in FRAMEWORK_FILES.values()}
    if not cycle_added:
        return None

    zbuf = _BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return (f"Booppa_Compliance_Bundle_{safe_co}_{date_str}.zip", zbuf.getvalue())


@celery_app.task(bind=True, max_retries=3, name="fulfill_cover_sheet_task")
def fulfill_cover_sheet_task(
    self,
    bundle_type: str,
    customer_email: str | None = None,
    company_name: str = "",
    metadata: dict | None = None,
):
    """
    Compliance Evidence Pack — Cover Sheet fulfillment.
    Runs 300s after bundle components are queued so reports have time to generate.
    Flow: SHA-256 hash → blockchain anchor → generate PDF → email delivery.
    """
    import hashlib
    import uuid as _uuid
    metadata = metadata or {}
    try:
        from app.services.cover_sheet_generator import generate_cover_sheet
        from app.services.storage import S3Service
        from app.services.email_service import EmailService
        from app.core.config import settings
        from app.core.models import User, Report

        # Stable across Celery retries: prefer caller-supplied id, then the
        # task's request id (preserved across retries), then a fresh uuid as
        # last resort. Stability matters because report_id is mixed into the
        # blockchain digest below — a per-retry uuid would re-anchor and burn
        # gas on every retry.
        report_id = (
            metadata.get("report_id")
            or (self.request.id if self.request and self.request.id else None)
            or str(_uuid.uuid4())
        )

        # 1. Look up anchored bundle notarizations + PDPA + RFP Complete for this user.
        # In the new compliance-evidence-pack flow, anchored_documents will be empty
        # at issue time — the user signs and notarizes the cover sheet AFTER receiving it.
        anchored_documents: list[dict] = []
        bcep_pack_id: str | None = None
        pdpa_score = metadata.get("pdpa_score", "—")
        pdpa_status = "Pending"
        rfp_status = "Pending"
        pdpa_tx_hash: str | None = None
        rfp_tx_hash: str | None = None
        signed_cs_tx: str | None = None
        signed_cs_hash: str | None = None
        explorer_base = settings.active_polygon_explorer_url.rstrip("/")
        pdpa_details: dict = {}
        rfp_details: dict = {}
        db = SessionLocal()
        try:
            user = (
                UserRepository.get_by_email(db, customer_email)
                if customer_email else None
            )
            if user:
                # ── ROPA Lite generation + anchoring (idempotent) ──────────
                # Generate + anchor the buyer's ROPA Lite BEFORE the
                # CYCLE_FRAMEWORKS query below runs, so the new ropa_lite Report
                # row is picked up as the bundle's 4th anchored document with no
                # changes to that query's logic. No-op if the buyer hasn't
                # submitted any ROPA rows, or if a ropa_lite Report already
                # exists (idempotent across retries).
                from app.core.models import RopaActivities
                existing_ropa_report = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework == "ropa_lite",
                        Report.tx_hash.isnot(None),
                    )
                    .first()
                )
                if not existing_ropa_report:
                    ropa_rows = (
                        db.query(RopaActivities)
                        .filter(
                            RopaActivities.user_id == user.id,
                            RopaActivities.status == "submitted",
                        )
                        .all()
                    )
                    if ropa_rows:
                        try:
                            from app.services.ropa_generator import generate_ropa_lite_pdf

                            ropa_dicts = [{
                                "processing_purpose": r.processing_purpose,
                                "data_categories": r.data_categories,
                                "data_subjects": r.data_subjects,
                                "retention_period": r.retention_period,
                                "cross_border_transfer": r.cross_border_transfer,
                                "legal_basis": r.legal_basis,
                            } for r in ropa_rows]

                            ropa_uen = getattr(user, "uen", None) or "Not provided"
                            # DPO name/email live on the most recent rfp_complete
                            # Report's assessment_data["intake_data"] (collected at
                            # RFP intake), not on the User model. Independent of
                            # ROPA's own intake — ROPA may be submitted before RFP.
                            rfp_report_for_dpo = (
                                db.query(Report)
                                .filter(
                                    Report.owner_id == user.id,
                                    Report.framework == "rfp_complete",
                                )
                                .order_by(Report.created_at.desc())
                                .first()
                            )
                            rfp_ad_for_dpo = (
                                rfp_report_for_dpo.assessment_data
                                if rfp_report_for_dpo and isinstance(rfp_report_for_dpo.assessment_data, dict)
                                else {}
                            )
                            rfp_intake_for_dpo = rfp_ad_for_dpo.get("intake_data") or {}

                            ropa_acra_data = asyncio.run(
                                fetch_acra_status(
                                    uen=ropa_uen if ropa_uen != "Not provided" else None,
                                    company_name=company_name
                                )
                            )

                            ropa_pdf_bytes = generate_ropa_lite_pdf(
                                company_name=company_name or "Your Organisation",
                                uen=ropa_uen,
                                acra_data=ropa_acra_data,
                                rows=ropa_dicts,
                                dpo_name=rfp_intake_for_dpo.get("dpo_name"),
                                dpo_email=rfp_intake_for_dpo.get("dpo_email"),
                            )
                            ropa_file_hash = hashlib.sha256(ropa_pdf_bytes).hexdigest()

                            ropa_s3 = S3Service()
                            ropa_report_id = f"ropa-lite-{user.id}"
                            ropa_s3_key = f"ropa/{ropa_report_id}.pdf"
                            ropa_s3.s3_client.put_object(
                                Bucket=ropa_s3.bucket, Key=ropa_s3_key,
                                Body=ropa_pdf_bytes, ContentType="application/pdf",
                            )

                            ropa_tx_hash = asyncio.run(
                                BlockchainService().anchor_evidence(
                                    ropa_file_hash, metadata=f"ropa_lite:{ropa_report_id}",
                                )
                            )

                            ropa_report = Report(
                                owner_id=user.id,
                                framework="ropa_lite",
                                company_name=company_name or "Your Organisation",
                                status="completed",
                                tx_hash=ropa_tx_hash,
                                audit_hash=ropa_file_hash,
                                completed_at=datetime.now(timezone.utc),
                                assessment_data={
                                    "file_hash": ropa_file_hash,
                                    "s3_key": ropa_s3_key,
                                    "row_count": len(ropa_dicts),
                                    "original_filename": f"ROPA_Lite_{user.id}.pdf",
                                    "blockchain_anchored_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                            db.add(ropa_report)
                            db.commit()
                            logger.info(
                                "[ROPA] Generated + anchored for %s (rows=%d, tx=%s)",
                                customer_email, len(ropa_dicts),
                                (ropa_tx_hash[:10] + "…") if ropa_tx_hash else "none",
                            )
                        except Exception as ropa_err:
                            # Non-fatal: deliver PDPA + RFP + Cover Sheet without
                            # ROPA rather than failing the whole cycle. Logged
                            # loudly — a missing ROPA on a paid bundle is a real
                            # gap to investigate, not something to swallow.
                            logger.error(
                                "[ROPA] Generation/anchoring FAILED for %s: %s. "
                                "Bundle delivered WITHOUT ROPA — investigate and re-trigger.",
                                customer_email, ropa_err,
                            )
                            db.rollback()

                # Anchored Compliance Documents is scoped to THIS cycle's
                # bundle artifacts only — PDPA scan PDF, RFP kit PDF, the
                # Cover Sheet itself (referenced below as the current report),
                # and any signed Cover Sheet from this cycle. We deliberately
                # exclude general compliance_notarization rows: those carry
                # the buyer's ad-hoc document uploads (across all projects
                # they've worked on) and leaking them onto a Cover Sheet the
                # buyer hands to a procurer/regulator looks unprofessional
                # and exposes unrelated work. The PDPA / RFP / signed-CS
                # anchors are pulled below from their dedicated Report
                # frameworks; this section dedupes them with proper
                # descriptors so the procurer sees only this bundle's
                # artifacts.
                CYCLE_FRAMEWORKS = (
                    "pdpa_quick_scan",
                    "rfp_complete",
                    "compliance_evidence_signed_sheet",
                    "ropa_lite",
                )
                cycle_rows = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework.in_(CYCLE_FRAMEWORKS),
                        Report.tx_hash.isnot(None),
                    )
                    .order_by(Report.created_at.desc())
                    .all()
                )
                # Keep only the most recent of each framework — this is the
                # current cycle's artifact. Anything older is a prior cycle.
                seen_frameworks: set[str] = set()
                FRAMEWORK_LABELS = {
                    "pdpa_quick_scan": "PDPA Quick Scan Report",
                    "rfp_complete": "RFP Complete Kit",
                    "compliance_evidence_signed_sheet": "Signed Cover Sheet",
                    "ropa_lite": "Record of Processing Activities (ROPA Lite)",
                }
                for r in cycle_rows:
                    if r.framework in seen_frameworks:
                        continue
                    seen_frameworks.add(r.framework)
                    ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
                    anchored_documents.append({
                        "filename": ad.get("original_filename") or FRAMEWORK_LABELS.get(r.framework, r.framework),
                        "descriptor": FRAMEWORK_LABELS.get(r.framework, r.framework),
                        "file_hash": ad.get("file_hash") or r.audit_hash or "—",
                        "tx_hash": r.tx_hash,
                        "tx_url": f"{explorer_base}/tx/{r.tx_hash}" if r.tx_hash else None,
                        "anchored_at": ad.get("blockchain_anchored_at")
                            or (r.completed_at.isoformat() if r.completed_at else None),
                    })
                # ── BCEP (Compliance Evidence Pack) — fold the 7 governance
                # documents into the anchored list so DOCUMENTS ANCHORED reflects
                # the full evidence set (forensic-audit finding: the 7 BCEP docs
                # were generated but never connected to the cover sheet).
                try:
                    from app.core.models import EvidencePack
                    from app.services.tx_utils import is_real_onchain_tx

                    _pack = (
                        db.query(EvidencePack)
                        .filter(
                            EvidencePack.user_id == user.id,
                            EvidencePack.status == "ready",
                        )
                        .order_by(EvidencePack.created_at.desc())
                        .first()
                    )
                    if _pack and isinstance(_pack.anchoring, dict):
                        bcep_pack_id = _pack.pack_id
                        BCEP_LABELS = {
                            "dpmp": "Data Protection Management Programme",
                            "ropa": "Record of Processing Activities (ROPA)",
                            "data_inventory": "Data Inventory & Retention Schedule",
                            "vendor_register": "Third-Party Processor Register & DPA Checklist",
                            "breach_runbook": "Data Breach Response Runbook",
                            "training": "Staff Training Register",
                            "review_log": "Periodic Security Review Log",
                        }
                        _bh = _pack.hashes if isinstance(_pack.hashes, dict) else {}
                        # Only list a doc the customer actually received. A doc
                        # can be anchored (hash written on-chain) yet fail to
                        # build/upload — listing it on the cover sheet as
                        # "anchored" while the buyer never got the file was the
                        # forensic finding (Security Review Log shown anchored,
                        # not delivered). Gate on presence in download_urls.
                        _dl = _pack.download_urls if isinstance(_pack.download_urls, dict) else {}
                        for dt, label in BCEP_LABELS.items():
                            _anc = _pack.anchoring.get(dt) if isinstance(_pack.anchoring.get(dt), dict) else {}
                            _tx = _anc.get("tx_hash")
                            if not is_real_onchain_tx(_tx):
                                continue  # only include confirmed real on-chain anchors
                            if not _dl.get(dt):
                                continue  # anchored but not delivered — never list
                            anchored_documents.append({
                                "filename": f"{label} ({_pack.pack_id})",
                                "descriptor": label,
                                "file_hash": _bh.get(dt) or "—",
                                "tx_hash": _tx,
                                "tx_url": f"{explorer_base}/tx/{_tx}",
                                "anchored_at": _anc.get("anchored_at"),
                            })
                except Exception as _bcep_err:
                    logger.warning("[CoverSheet] BCEP link failed (non-blocking): %s", _bcep_err)

                # PDPA + VP status from latest matching report.
                # Deliverable-selection guard (forensic finding: an empty-score
                # QA artifact — "Vendor: Test", suite-b.booppa.io, all scores
                # "—" — was picked up as the cover sheet's PDPA source). Only a
                # *completed*, *real-scored*, *non-test* scan may back a paying
                # customer's deliverable. Scan the newest-first candidates and
                # take the first that qualifies rather than blindly taking the
                # latest row (which could be a stub or a scan that produced no
                # dimension scores).
                _pdpa_candidates = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                        Report.status == "completed",
                    )
                    .order_by(Report.created_at.desc())
                    .limit(10)
                    .all()
                )
                pdpa_report = None
                for _cand in _pdpa_candidates:
                    _cad = _cand.assessment_data if isinstance(_cand.assessment_data, dict) else {}
                    if resolve_pdpa_score(_cad) is None:
                        continue  # empty-score scan — not a deliverable
                    pdpa_report = _cand
                    break
                if pdpa_report:
                    pdpa_status = pdpa_report.status.title() if pdpa_report.status else "Pending"
                    pdpa_tx_hash = pdpa_report.tx_hash
                    pdpa_ad = pdpa_report.assessment_data if isinstance(pdpa_report.assessment_data, dict) else {}
                    structured = pdpa_ad.get("booppa_report") if isinstance(pdpa_ad.get("booppa_report"), dict) else {}
                    # Single source of truth: `resolve_pdpa_score` returns the
                    # persisted `compliance_score` verbatim when present (never
                    # recompute — that's what drifted the cover sheet to 54 while
                    # the PDPA report showed 53), else derives it from raw risk.
                    # The RFP Supplier Declaration reads the same helper via
                    # `latest_pdpa_score`, so the two documents can't disagree.
                    canonical_score = pdpa_ad.get("compliance_score")
                    _resolved_score = resolve_pdpa_score(pdpa_ad)
                    pdpa_score = _resolved_score if _resolved_score is not None else "—"
                    # Single source of truth — same resolver the Monitor Report
                    # uses, so the two documents can never disagree on the count.
                    findings = resolve_pdpa_findings(pdpa_ad)

                    sev_counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
                    for f in findings:
                        if not isinstance(f, dict):
                            continue
                        sev = (f.get("severity") or "").title()
                        if sev in sev_counts:
                            sev_counts[sev] += 1

                    # Score-vs-findings consistency check: if the AI reported
                    # a clean score but enumerated findings, derive the score
                    # from the findings count using PDPC's standard weighting
                    # (Critical=25, High=15, Medium=8, Low=3) so the displayed
                    # number can't contradict the rendered findings list. The
                    # raw `pdpa_score` derived from raw_risk takes precedence
                    # only when findings are empty (clean site).
                    # Only fall back to a findings-derived score when the PDPA
                    # report did NOT persist a canonical compliance_score (older
                    # reports). When it did, that value is authoritative and must
                    # not be overridden — otherwise the cover sheet and the PDPA
                    # report disagree.
                    if findings and not isinstance(canonical_score, (int, float)):
                        weighted_risk = (
                            sev_counts["Critical"] * 25
                            + sev_counts["High"] * 15
                            + sev_counts["Medium"] * 8
                            + sev_counts["Low"] * 3
                        )
                        derived_score = max(0, min(100, 100 - weighted_risk))
                        # Trust the findings-derived score when it disagrees
                        # materially with the raw AI score — a 100/100 with
                        # 3 High findings is the bug we're guarding against.
                        if not isinstance(pdpa_score, int) or abs(pdpa_score - derived_score) >= 10:
                            pdpa_score = derived_score
                            logger.info(
                                f"[CoverSheet] PDPA score derived from {len(findings)} findings "
                                f"({sev_counts}) → {derived_score}; raw was inconsistent"
                            )

                    # Risk level: derive from worst finding severity if AI didn't
                    # set one, so "Risk Level —" doesn't appear next to actual
                    # High/Critical findings.
                    risk_level = pdpa_ad.get("risk_level") or structured.get("risk_level")
                    if not risk_level or risk_level == "—":
                        if sev_counts["Critical"] > 0:
                            risk_level = "Critical"
                        elif sev_counts["High"] > 0:
                            risk_level = "High"
                        elif sev_counts["Medium"] > 0:
                            risk_level = "Medium"
                        elif sev_counts["Low"] > 0:
                            risk_level = "Low"
                        else:
                            risk_level = "Minimal"

                    # Scan scope data — what was actually crawled and when.
                    # Disclosure is the single most powerful credibility
                    # signal for an auditor: it shows the report is honest
                    # about its limits. Pull what's persisted; fall back to
                    # static defaults that are accurate for any web-only
                    # PDPA scan (we never log into authenticated areas).
                    scan_scope = {
                        "pages_crawled": (
                            pdpa_ad.get("pages_crawled")
                            or pdpa_ad.get("page_count")
                            or structured.get("pages_crawled")
                        ),
                        "started_at": pdpa_ad.get("scan_started_at") or (
                            pdpa_report.created_at.isoformat() if pdpa_report.created_at else None
                        ),
                        "completed_at": pdpa_report.completed_at.isoformat() if pdpa_report.completed_at else None,
                        "ssl_grade": pdpa_ad.get("ssl_grade") or structured.get("ssl_grade"),
                        "ssl_grade_checked_at": pdpa_ad.get("ssl_grade_checked_at"),
                        "excluded": [
                            "Authenticated areas (login-gated routes)",
                            "API endpoints",
                            "Mobile applications",
                            "Subdomains not crawled from the seed URL",
                        ],
                        "scanner_version": pdpa_ad.get("scanner_version") or "Booppa PDPA Scanner v1",
                    }

                    pdpa_details = {
                        # Prefer the exact URL the PDPA report PDF displayed
                        # (persisted as display_url) so both documents in the
                        # bundle name the same scanned URL — no crayon.com vs
                        # crayon.com/sg mismatch.
                        "website_url": (
                            pdpa_ad.get("display_url")
                            or pdpa_ad.get("website_url")
                            or pdpa_report.company_website
                            or "—"
                        ),
                        "risk_level": risk_level,
                        "total_findings": len(findings),
                        "severity_counts": sev_counts,
                        # Full findings list — cover sheet renders each with
                        # severity, description, legislation, recommendation.
                        # No truncation here: the cover sheet is the customer's
                        # full evidence record, not a teaser.
                        "findings": findings,
                        "executive_summary": structured.get("executive_summary") or pdpa_report.ai_narrative or "",
                        "detected_laws": pdpa_ad.get("detected_laws") or [],
                        "scanned_at": pdpa_report.completed_at.isoformat() if pdpa_report.completed_at else None,
                        "scan_scope": scan_scope,
                    }
                rfp_report = (
                    db.query(Report)
                    .filter(Report.owner_id == user.id, Report.framework == "rfp_complete")
                    .order_by(Report.created_at.desc())
                    .first()
                )
                if rfp_report:
                    rfp_status = rfp_report.status.title() if rfp_report.status else "Pending"
                    rfp_tx_hash = rfp_report.tx_hash
                    rfp_ad = rfp_report.assessment_data if isinstance(rfp_report.assessment_data, dict) else {}
                    rfp_details = {
                        "product_type": rfp_ad.get("product_type") or "rfp_complete",
                        "qa_count": rfp_ad.get("qa_count"),
                        # Full Q&A list embedded in the cover sheet — see the
                        # webhook persistence change that started storing the
                        # full list (not just qa_count) on the Report row.
                        "qa_answers": rfp_ad.get("qa_answers") or [],
                        "answer_source": rfp_ad.get("answer_source"),
                        "generated_at": rfp_ad.get("generated_at") or (
                            rfp_report.completed_at.isoformat() if rfp_report.completed_at else None
                        ),
                        "download_url": rfp_ad.get("download_url"),
                        "discrepancies": rfp_ad.get("discrepancies") or [],
                        "executive_summary": rfp_ad.get("executive_summary") or rfp_report.ai_narrative or "",
                    }
                else:
                    # Backfill: some RFP completions predate the unconditional
                    # Report-row write. CertificateLog is always written, so
                    # use it as evidence the RFP finished.
                    try:
                        from app.core.models import CertificateLog
                        cert = (
                            db.query(CertificateLog)
                            .filter(
                                CertificateLog.vendor_id == user.id,
                                CertificateLog.certificate_type == "RFP",
                            )
                            .order_by(CertificateLog.generated_at.desc())
                            .first()
                        )
                        if cert:
                            rfp_status = "Completed"
                            rfp_details = {
                                "product_type": "rfp_complete",
                                "generated_at": cert.generated_at.isoformat() if cert.generated_at else None,
                            }
                    except Exception as e:
                        logger.warning(f"[CoverSheet] RFP CertificateLog fallback failed: {e}")
                        try:
                            db.rollback()
                        except Exception:
                            pass
                # Signed cover sheet upload — only present in regen-after-signing pass.
                # Scope to THIS cycle: the signed sheet must be newer than the
                # latest PDPA report. Otherwise a previous month's signed sheet
                # would leak into the current cycle's cover sheet + final email.
                signed_q = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework == "compliance_evidence_signed_sheet",
                    )
                )
                if pdpa_report and pdpa_report.created_at:
                    signed_q = signed_q.filter(Report.created_at >= pdpa_report.created_at)
                signed_report = signed_q.order_by(Report.created_at.desc()).first()
                if signed_report:
                    signed_cs_tx = signed_report.tx_hash
                    s_ad = signed_report.assessment_data if isinstance(signed_report.assessment_data, dict) else {}
                    signed_cs_hash = s_ad.get("file_hash") or signed_report.audit_hash
        finally:
            db.close()

        # 1b. Readiness gate — defer until PDPA + RFP Complete are both done.
        # Notarization is intentionally NOT a precondition: the user receives the
        # cover sheet first, signs it, and then notarizes it.
        force = bool(metadata.get("force"))
        pdpa_done = pdpa_status.lower() == "completed"
        rfp_done = rfp_status.lower() == "completed"
        if not force and (not pdpa_done or not rfp_done):
            try:
                db_r = SessionLocal()
                u = db_r.query(User).filter(User.email == customer_email).first() if customer_email else None
                if u and not getattr(u, "pending_cover_sheet", False):
                    u.pending_cover_sheet = True
                    db_r.commit()
                db_r.close()
            except Exception:
                pass
            countdown = min(600, 60 * (self.request.retries + 1))
            logger.info(
                f"[CoverSheet] Deferring — pdpa_done={pdpa_done} rfp_done={rfp_done} "
                f"retry={self.request.retries} in {countdown}s"
            )
            raise self.retry(countdown=countdown)

        # Derive Recommendations from the actual PDPA findings, sorted by
        # severity. A buyer reading the boilerplate "Address any PDPA gaps
        # identified in your scan report within 30 days" alongside 3 specific
        # findings looks lazy. A buyer reading "Add a 'Purpose' clause to your
        # privacy policy (PDPA s. 20) — 30-day deadline" + the other 2
        # specifics has actionable next steps.
        SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        SEVERITY_SLA = {
            "CRITICAL": "immediate",
            "HIGH": "30 days",
            "MEDIUM": "60 days",
            "LOW": "90 days",
            "INFO": "next quarter",
        }
        findings_list = (pdpa_details.get("findings") or []) if pdpa_details else []
        ranked = sorted(
            (f for f in findings_list if isinstance(f, dict)),
            key=lambda f: SEVERITY_RANK.get((f.get("severity") or "").upper(), 9),
        )[:5]
        if ranked:
            derived_recs: list[str] = []
            for f in ranked:
                title = (f.get("title") or f.get("type") or "PDPA finding").strip()
                rec_text = (f.get("recommendation") or f.get("remediation") or "").strip()
                sev = (f.get("severity") or "").upper()
                sla = SEVERITY_SLA.get(sev, "30 days")
                lawref = (f.get("legislation_text") or "").split(";")[0].strip()
                lead = rec_text if rec_text else f"Address: {title}"
                tail_parts: list[str] = []
                if lawref:
                    tail_parts.append(lawref)
                tail_parts.append(f"{sev or 'PDPA'} · remediate within {sla}")
                derived_recs.append(f"{lead} ({' · '.join(tail_parts)})")
            recommendations = derived_recs
        else:
            recommendations = None  # cover_sheet_generator falls back to its boilerplate

        # 2. Build cover data
        cover_data = {
            "report_id": report_id,
            "bundle_type": bundle_type,
            "company_name": company_name or metadata.get("company_name", ""),
            "customer_email": customer_email,
            "pdpa_status": pdpa_status,
            "pdpa_score": pdpa_score,
            "pdpa_details": pdpa_details,
            "pdpa_tx_hash": pdpa_tx_hash,
            "rfp_status": rfp_status,
            "rfp_details": rfp_details,
            "rfp_tx_hash": rfp_tx_hash,
            "signed_cs_tx": signed_cs_tx,
            "signed_cs_hash": signed_cs_hash,
            "notarization_count": len(anchored_documents),
            "anchored_documents": anchored_documents,
            "tx_hash": "—",
            "network": settings.active_polygon_network_name,
            "recommendations": recommendations,
            "trm_domains": [],
            "bcep_pack_id": bcep_pack_id,
            # Explicit PDPC coverage statement (forensic-audit finding: the bundle
            # must state which PDPC levels it covers for the auditor/buyer/inspector).
            "pdpc_coverage": (
                "This bundle covers PDPC Compliance Levels 1 and 2 — automated website "
                "evidence (Level 1) and documented data-processing activities / ROPA "
                "(Level 2). For Levels 3–6, see the Compliance Evidence Pack."
            ),
        }

        # 3. Anchor the cover sheet itself (digest of the included evidence).
        # Cached so retries (PDF render or S3 upload failures) don't re-anchor
        # and burn another Polygon tx — anchor once per report_id.
        from app.core.cache import cache as _cache
        anchor_cache_key = _cache.cache_key(f"cover_sheet_anchor:{report_id}")
        try:
            cached = _cache.get(anchor_cache_key)
            if cached and cached.get("tx"):
                cover_data["tx_hash"] = cached["tx"]
            else:
                digest_input = "|".join(
                    [d.get("file_hash", "") for d in anchored_documents] + [report_id]
                )
                content_hash = hashlib.sha256(digest_input.encode()).hexdigest()
                blockchain = BlockchainService()
                tx = asyncio.run(blockchain.anchor_evidence(content_hash, metadata=f"cover_sheet:{report_id}"))
                if tx:
                    cover_data["tx_hash"] = tx
                    _cache.set(anchor_cache_key, {"tx": tx}, ttl=86400)
        except Exception as e:
            logger.warning(f"Cover sheet blockchain anchor failed (non-blocking): {e}")

        # 4. Generate PDF
        pdf_bytes = generate_cover_sheet(cover_data)

        # 5. Upload to S3 (sync put_object since we're in a sync Celery task)
        s3 = S3Service()
        s3_key = f"cover_sheets/{report_id}/compliance_cover_sheet.pdf"
        s3.s3_client.put_object(
            Bucket=s3.bucket,
            Key=s3_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            Metadata={"report-id": str(report_id), "kind": "cover-sheet"},
        )
        download_url = s3.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3.bucket, "Key": s3_key},
            ExpiresIn=604800,  # 7 days
        )

        # 5b. Persist a Report row so the frontend can poll for completion + re-presign on demand
        cs_report_id: str | None = None
        if user:
            db2 = None
            try:
                from app.services.cover_sheet_generator import COVER_SHEET_SCHEMA_VERSION
                db2 = SessionLocal()
                cs_report = Report(
                    owner_id=user.id,
                    framework="compliance_evidence_pack",
                    company_name=company_name or "Your Organisation",
                    assessment_data={
                        "bundle_type": bundle_type,
                        "s3_key": s3_key,
                        "schema_version": COVER_SHEET_SCHEMA_VERSION,
                        "anchored_count": len(anchored_documents),
                        "bcep_pack_id": bcep_pack_id,
                        "pdpa_status": pdpa_status,
                        "pdpa_score": pdpa_score if isinstance(pdpa_score, int) else None,
                        "rfp_status": rfp_status,
                        "rfp_download_url": rfp_details.get("download_url"),
                    },
                    status="completed",
                    tx_hash=cover_data.get("tx_hash") if cover_data.get("tx_hash") != "—" else None,
                    file_key=s3_key,
                    s3_url=download_url,
                    completed_at=datetime.now(timezone.utc),
                )
                db2.add(cs_report)
                db2.commit()
                cs_report_id = str(cs_report.id)
            except Exception as persist_err:
                logger.warning(f"Cover sheet Report persist failed (non-blocking): {persist_err}")
                if db2 is not None:
                    try:
                        db2.rollback()
                    except Exception:
                        pass
            finally:
                if db2 is not None:
                    db2.close()

        # 6. Email delivery — branch on whether signed CS hash is present.
        # Idempotency is CONTENT-AWARE: keyed on (email, signed-state, schema
        # version, and a fingerprint of the anchored documents). This suppresses
        # the genuine re-flood cases — the hourly `sweep_pending_cover_sheets`
        # backstop and Celery retries, which re-fire with the SAME underlying
        # PDPA/RFP documents — while still delivering a fresh email when the
        # content actually changes (a new scan produces new document hashes, or
        # the cover sheet's visible structure changes and COVER_SHEET_SCHEMA_VERSION
        # is bumped per CLAUDE.md). 24h TTL bounds the suppression window.
        if customer_email:
            delivery_email = customer_email
            if user:
                try:
                    from app.core.models import EvidencePack
                    _pack = (
                        db.query(EvidencePack)
                        .filter(EvidencePack.user_id == user.id)
                        .order_by(EvidencePack.created_at.desc())
                        .first()
                    )
                    if _pack and _pack.contact_email:
                        delivery_email = _pack.contact_email
                except Exception as e:
                    logger.warning(f"Could not resolve contact_email for {customer_email}: {e}")

            email_state = "signed" if signed_cs_tx else "unsigned"
            try:
                from app.services.cover_sheet_generator import COVER_SHEET_SCHEMA_VERSION as _cs_ver
            except Exception:
                _cs_ver = "x"
            # Fingerprint the anchored documents (PDPA + RFP file hashes). A new
            # scan/cycle yields different hashes → new key → email delivers; an
            # unchanged cycle (sweep/retry) yields the same key → suppressed.
            _doc_hashes = sorted(
                str(d.get("file_hash") or "")
                for d in (anchored_documents or [])
                if d.get("file_hash")
            )
            _docs_fp = hashlib.sha256("|".join(_doc_hashes).encode()).hexdigest()[:12] if _doc_hashes else "nodocs"
            # test_simulation (admin test-checkout) always re-sends so iteration
            # isn't blocked by the 24h guard.
            _is_test = bool((metadata or {}).get("test_simulation"))
            email_dedupe_key = _cache.cache_key(
                f"cover_sheet_email:{delivery_email}:{email_state}:v{_cs_ver}:{_docs_fp}"
            )
            if not _is_test and _cache.get(email_dedupe_key):
                logger.info(
                    f"[CoverSheet] Skipping duplicate email to {delivery_email} "
                    f"(state={email_state}, schema=v{_cs_ver}, docs={_docs_fp}, already sent within 24h)"
                )
                return

            email_svc = EmailService()
            explorer = settings.active_polygon_explorer_url.rstrip("/")

            def _tx_link(label: str, tx: str | None) -> str:
                if not tx or tx == "—":
                    return f"<li>{label}: <em>pending</em></li>"
                return (
                    f"<li>{label}: "
                    f"<a href='{explorer}/tx/{tx}'>"
                    f"<code style='font-size:12px;'>{tx[:12]}…{tx[-8:]}</code></a></li>"
                )

            cs_anchor_tx = cover_data.get("tx_hash") if cover_data.get("tx_hash") != "—" else None

            # Stable download URL — the presigned S3 link expires when the
            # signing STS credentials rotate (~hours on EC2/ECS roles), well
            # before the 7-day URL TTL. Route through the backend redirect
            # endpoint so clicks always get a fresh presigned URL.
            if cs_report_id:
                email_download_url = (
                    f"https://api.booppa.io/api/v1/compliance/cover-sheet/download/{cs_report_id}"
                )
            else:
                email_download_url = download_url

            # Reserve the dedupe slot BEFORE sending so a concurrent
            # second invocation can't race past the get() check.
            _cache.set(
                email_dedupe_key,
                {"reserved_at": datetime.now(timezone.utc).isoformat()},
                ttl=86400,
            )

            if signed_cs_tx:
                # FINAL receipt: signed cover sheet has been uploaded + anchored.
                # Bundle every document into a single evidence ZIP the buyer can
                # forward to a reviewer (best-effort — never blocks the receipt).
                _zip_attachment = None
                try:
                    if user:
                        _zip = _build_compliance_bundle_zip(db, user.id, company_name, pdf_bytes)
                        if _zip:
                            _zip_attachment = [_zip]
                except Exception as _zip_err:
                    logger.warning("[CoverSheet] evidence ZIP build failed (non-blocking): %s", _zip_err)

                from app.services.email_layout import branded_email_html, email_button, email_info_box
                asyncio.run(email_svc.send_html_email(
                    to_email=delivery_email,
                    subject="Your Compliance Evidence Pack — final blockchain receipt",
                    attachments=_zip_attachment,
                    body_html=branded_email_html(
                        f"<h2 style='margin:0 0 12px;font-size:20px;color:#0f172a;'>Compliance Evidence Pack — complete</h2>"
                        f"<p style='margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;'>Your signed Cover Sheet is anchored on "
                        f"{settings.active_polygon_network_name}. Download the regenerated cover sheet — "
                        f"Section 5 now lists every blockchain anchor below.</p>"
                        + email_button(email_download_url, "Download Updated Cover Sheet")
                        + email_info_box(
                            "<strong>Blockchain anchors</strong>"
                            "<ul style='margin:8px 0 0;padding-left:20px;'>"
                            f"{_tx_link('PDPA Snapshot', pdpa_tx_hash)}"
                            f"{_tx_link('RFP Complete Kit', rfp_tx_hash)}"
                            f"{_tx_link('Cover Sheet (issued)', cs_anchor_tx)}"
                            f"{_tx_link('Cover Sheet (signed)', signed_cs_tx)}"
                            "</ul>",
                            tone="success",
                        )
                        + (
                            "<p style='margin:0 0 16px;color:#334155;font-size:13px;line-height:1.6;'>📎 Your complete evidence archive "
                            "(cover letter + Cover Sheet + PDPA Snapshot + RFP Kit + ROPA) is attached "
                            "as a single ZIP — forward it to enterprise buyers, procurement teams, or the PDPC.</p>"
                            if _zip_attachment else ""
                        )
                        + "<p style='margin:0;color:#64748b;font-size:13px;'>Keep this email — the four anchors above are your full audit trail.</p>",
                        title="Compliance Evidence Pack — complete",
                        preheader="Your signed Cover Sheet is anchored — full audit trail inside.",
                    ),
                ))
                logger.info(
                    f"[CoverSheet] Final receipt sent to {customer_email} "
                    f"(signed_cs={signed_cs_tx[:10]}…, zip={'yes' if _zip_attachment else 'no'})"
                )
            else:
                _draft_attachment = None
                if pdf_bytes:
                    _safe_co = (company_name or "Kit").replace("/", "-").replace(" ", "-")
                    _draft_attachment = [(f"Cover_Sheet_Unsigned_{_safe_co}.pdf", pdf_bytes)]

                from app.services.email_layout import branded_email_html, email_button, email_info_box
                asyncio.run(email_svc.send_html_email(
                    to_email=delivery_email,
                    subject="Your Compliance Evidence Pack — sign & notarize the Cover Sheet",
                    attachments=_draft_attachment,
                    body_html=branded_email_html(
                        f"<h2 style='margin:0 0 12px;font-size:20px;color:#0f172a;'>Cover Sheet ready — final step inside</h2>"
                        f"<p style='margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;'>Your PDPA Snapshot and RFP Complete kit are anchored on "
                        f"{settings.active_polygon_network_name}. The 9-section regulator-ready Cover Sheet below "
                        f"summarises both and is itself anchored on-chain.</p>"
                        + email_button(email_download_url, "Download Cover Sheet PDF")
                        + email_info_box(
                            "<strong>Final step</strong>"
                            "<ol style='margin:8px 0 0;padding-left:20px;'>"
                            "<li>Sign the downloaded PDF (digital or wet signature).</li>"
                            "<li>Go to <a href='https://www.booppa.io/compliance/cover-sheet'>booppa.io/compliance/cover-sheet</a> and upload the signed copy.</li>"
                            "<li>Your 1 included notarization credit is auto-applied — no payment.</li>"
                            "</ol>",
                            tone="warn",
                        )
                        + "<p style='margin:0;color:#64748b;font-size:13px;'>You'll receive a final blockchain receipt once the signed Cover Sheet is anchored.</p>",
                        title="Cover Sheet ready — final step",
                        preheader="Sign and upload your Cover Sheet to complete your Evidence Pack.",
                    ),
                ))
                logger.info(f"Cover sheet delivered to {customer_email} for {bundle_type} — awaiting signed upload")

    except Retry:
        # Readiness-gate retry — let Celery handle it without overriding the countdown.
        raise
    except (NameError, TypeError, AttributeError, KeyError, ImportError, SyntaxError) as exc:
        # Deterministic code bugs — retrying 20× over 2.5h will fail identically
        # every time and clog the queue (and waste the readiness/anchor work
        # already done this attempt). Fail fast so an oncall can ship a fix.
        logger.exception(f"Cover sheet fulfillment failed permanently (code bug): {exc}")
        return
    except Exception as exc:
        logger.error(f"Cover sheet fulfillment failed: {exc}")
        countdown = 120 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=2, name="vendor_active_health_check_task")
def vendor_active_health_check_task(self, vendor_id: str, vendor_email: str, override_company: str | None = None, is_first_cycle: bool = False):
    """
    Single consolidated digest for Vendor Active / Vendor Pro subscribers.

    This is the ONE email these tiers send per cycle (the bare "Activated" email
    is suppressed for them in `_activate_subscription`). It bundles every tangible
    artifact + an evidence-of-features section so the buyer isn't left with a
    one-line email:
      • Recalculated Trust + Compliance scores and 30-day profile views
      • A downloadable one-page status snapshot PDF (badge + scores)
      • GeBIZ tender alerts closing soon (the "real-time GeBIZ alerts" feature)
      • An itemised checklist of what their tier unlocks, with dashboard links
      • Vendor Pro only: the included monthly notarization + a note that the
        PDPA Snapshot/drift report follows in a second email when the scan ends

    `is_first_cycle=True` (set by the activation wrappers) switches the framing
    from "monthly digest" to "welcome". `override_company` is test-harness-only
    (admin Test Identity) and never mutates the stored profile.
    """
    db = SessionLocal()
    try:
        from app.services.scoring import VendorScoreEngine
        from app.services.email_service import EmailService
        from app.core.models import VendorScore, User
        from datetime import timedelta

        # 1. Recalculate score
        score_record = VendorScoreEngine.update_vendor_score(db, vendor_id)

        # 2. Build metrics summary
        user = UserRepository.get_by_id(db, str(vendor_id))
        company = (override_company or "").strip() or (getattr(user, "company", "Your company") if user else "Your company")

        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        from app.core.models import VerifyRecord, ProofView
        from app.core.repositories.verify_record_repository import VerifyRecordRepository
        verify = VerifyRecordRepository.get_by_vendor_id(db, str(vendor_id))
        profile_views = 0
        if verify:
            profile_views = db.query(ProofView).filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= thirty_days_ago,
            ).count()

        # 2a-insights. Trend, sector benchmark, and personalised tender matches —
        # the substance that makes the digest worth the subscription. All
        # best-effort (helpers swallow their own errors and return None/[]).
        from app.services.vendor_active_insights import (
            get_score_trend, get_sector_benchmark, get_tender_matches,
            get_trust_breakdown, get_sector_rank, get_search_impressions_30d,
        )
        trend = get_score_trend(db, vendor_id)
        benchmark = get_sector_benchmark(db, vendor_id)
        # B.4: surface a win-probability on the Active snapshot too (not just Pro
        # email), so the snapshot PDF shows a differentiated win% column.
        tender_matches = get_tender_matches(db, vendor_id, limit=5, with_win_probability=True)
        # 4b/4e: per-dimension Trust Score breakdown + absolute sector rank.
        trust_breakdown = get_trust_breakdown(db, vendor_id)
        sector_rank = get_sector_rank(db, vendor_id)
        # Search-impression count (trailing 30d) → "appeared in N searches".
        search_impressions_30d = get_search_impressions_30d(db, vendor_id)

        # 2b. Render a one-page status snapshot PDF so the monthly email links a
        # real, fileable/forwardable artifact instead of being email-only.
        plan = (getattr(user, "plan", "") or "")
        plan_label = "Vendor Pro" if plan == "vendor_pro" else "Vendor Active"
        snapshot_url = None
        snapshot_pdf = None
        try:
            from app.services.vendor_snapshot_generator import generate_vendor_snapshot_pdf
            from app.services.storage import S3Service

            # Notarization visibility: surface the vendor's on-chain notarization
            # count so completed notarizations are visible in the snapshot rather
            # than only affecting hidden elevation logic. Best-effort — never
            # blocks the snapshot.
            snapshot_extra_rows = []
            try:
                from app.core.models import Proof, VerifyRecord
                _notal_count = (
                    db.query(Proof)
                    .join(VerifyRecord)
                    .filter(VerifyRecord.vendor_id == vendor_id)
                    .count()
                )
                if _notal_count > 0:
                    snapshot_extra_rows.append((
                        "Notarizations",
                        f"{_notal_count} record{'s' if _notal_count != 1 else ''} "
                        "anchored on-chain",
                    ))
            except Exception as _notal_err:
                logger.warning(
                    "[VendorSnapshot] notarization count failed for %s: %s",
                    vendor_id, _notal_err,
                )

            snapshot_pdf = generate_vendor_snapshot_pdf({
                "company_name": company,
                "plan_label": plan_label,
                "trust_score": getattr(score_record, "total_score", None),
                "compliance_score": getattr(score_record, "compliance_score", None),
                "profile_views_30d": profile_views,
                "verification_level": getattr(verify, "verification_level", None),
                "trend": trend,
                "sector_benchmark": benchmark,
                "trust_breakdown": trust_breakdown,
                "sector_rank": sector_rank,
                "search_impressions_30d": search_impressions_30d,
                "tender_matches": tender_matches,
                "extra_rows": snapshot_extra_rows,
            })
            snapshot_url = asyncio.run(
                S3Service().upload_pdf(snapshot_pdf, f"vendor-snapshot-{vendor_id}")
            )
        except Exception as snap_err:
            logger.warning(f"[VendorSnapshot] PDF/upload failed for {vendor_id}: {snap_err}")

        snapshot_cta = (
            f"""<p><a href="{snapshot_url}"
                     style="background:#0f172a;color:#fff;padding:11px 22px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                Download your status snapshot (PDF) ↓
              </a></p>"""
            if snapshot_url else ""
        )

        is_pro = (plan == "vendor_pro")

        # Vendor Pro: upgrade matches to include win-probability, and pull the
        # premium-only competitor pulse + PDPA drift for the email + attachments.
        competitor_pulse = None
        pdpa_drift = None
        if is_pro:
            from app.services.vendor_active_insights import get_competitor_pulse, get_pdpa_drift
            try:
                tender_matches = get_tender_matches(db, vendor_id, limit=8, with_win_probability=True)
            except Exception:
                pass
            competitor_pulse = get_competitor_pulse(db, vendor_id)
            pdpa_drift = get_pdpa_drift(db, vendor_id)

        def _esc(s) -> str:
            return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # 2c. Personalised tender matches — the "real-time GeBIZ alerts" feature
        # upgraded to per-vendor BID/WATCH/PASS recommendations (reuses the
        # Tender Intelligence classifier). Falls back to a plain closing-soon
        # list when the vendor has no sector/history (label None).
        gebiz_section = ""
        try:
            from app.services.tender_service_bid_classifier import bid_label_to_html_badge

            if tender_matches:
                show_wp = any(t.get("win_probability") is not None for t in tender_matches)
                rows = ""
                for t in tender_matches:
                    cd = t.get("closing_date")
                    close = cd.strftime("%d %b %Y") if cd else "—"
                    title = _esc((t.get("title") or "")[:90])
                    url = t.get("url")
                    cell = f'<a href="{_esc(url)}" style="color:#0ea5e9;text-decoration:none;">{title}</a>' if url else title
                    label = t.get("bid_label")
                    badge = bid_label_to_html_badge(label) if label else ""
                    wp = t.get("win_probability")
                    wp_td = (f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;white-space:nowrap;text-align:right;font-size:12px;color:#0f172a;font-weight:bold;">{wp}%</td>'
                             if show_wp else "")
                    rows += (
                        f'<tr><td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:13px;">{cell}</td>'
                        f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;white-space:nowrap;font-size:12px;color:#475569;">{close}</td>'
                        f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;white-space:nowrap;text-align:right;">{badge}</td>{wp_td}</tr>'
                    )
                wp_th = ('<th style="text-align:right;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Win %</th>'
                         if show_wp else "")
                heading = ("Tender matches — should you bid?" if any(t.get("bid_label") for t in tender_matches)
                           else "GeBIZ tender alerts — closing soon")
                gebiz_section = f"""
                <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">{heading}</h3>
                <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eef2f7;border-radius:8px;overflow:hidden;">
                  <tr style="background:#f8fafc;"><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Tender</th><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Closes</th><th style="text-align:right;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Signal</th>{wp_th}</tr>
                  {rows}
                </table>
                <p style="font-size:12px;color:#64748b;margin:6px 0 0;">
                  Signals are data-driven guidance from real GeBIZ history, not guarantees.
                  <a href="https://www.booppa.io/vendor/dashboard" style="color:#0ea5e9;">See all tender alerts →</a>
                </p>
                """
        except Exception as gebiz_err:
            logger.warning(f"[VendorDigest] tender matches section failed for {vendor_id}: {gebiz_err}")

        # 2d. Feature checklist — evidence of what the tier unlocks (some are
        # in-app entitlements, not files: badge, search priority, dashboards).
        active_feats = [
            ("Active badge on your public BOOPPA profile", "https://www.booppa.io/vendor/dashboard"),
            ("Priority placement in procurement searches", "https://www.booppa.io/vendor/dashboard"),
            ("Real-time GeBIZ tender alerts (above)", None),
            ("Unlimited win-probability checks", "https://www.booppa.io/tender-check"),
        ]
        pro_feats = [
            ("Quarterly PDPA Snapshot with drift tracking", "https://www.booppa.io/vendor/dashboard"),
            ("1 notarization included this month", "https://www.booppa.io/notarize"),
            ("Tender analytics dashboard (lite)", "https://www.booppa.io/vendor/dashboard"),
            ("Competitor awareness signals", "https://www.booppa.io/vendor/dashboard"),
        ]
        feats = active_feats + (pro_feats if is_pro else [])
        feat_items = "".join(
            f'<li style="margin:6px 0;">✓ ' +
            (f'<a href="{lnk}" style="color:#0f172a;">{label}</a>' if lnk else label) + '</li>'
            for label, lnk in feats
        )
        features_section = f"""
        <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">What your {plan_label} subscription includes</h3>
        <ul style="list-style:none;padding:0;margin:0;font-size:13px;color:#334155;">{feat_items}</ul>
        """

        # 2e. Vendor Pro: surface the included notarization + the second-email note.
        pro_note = ""
        if is_pro:
            from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS
            notar_limit = ENTERPRISE_NOTARIZATION_LIMITS.get(plan, 1)
            scan_line = (
                "<p style=\"font-size:13px;color:#334155;\">Your PDPA Snapshot with drift "
                "is being generated now — it arrives in a separate email shortly.</p>"
                if is_first_cycle else ""
            )
            pro_note = f"""
            <div style="background:#f0fdf4;border-left:3px solid #10b981;padding:12px 16px;border-radius:4px;margin:20px 0;">
              <strong>{notar_limit} notarization{'s' if notar_limit != 1 else ''} included this month.</strong>
              <a href="https://www.booppa.io/notarize" style="color:#10b981;">Redeem now →</a>
            </div>
            {scan_line}
            """

        # 2e-pro. Competitor pulse — Pro-only sector intelligence in the email.
        competitor_section = ""
        if is_pro and competitor_pulse and competitor_pulse.get("top_suppliers"):
            sup_rows = ""
            for sup in competitor_pulse["top_suppliers"][:5]:
                name = _esc(sup.get("name") or sup.get("supplier") or "—")
                wins = sup.get("count") or sup.get("wins") or "—"
                sup_rows += (
                    f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eef2f7;font-size:13px;">{name}</td>'
                    f'<td style="padding:6px 8px;border-bottom:1px solid #eef2f7;text-align:right;font-size:12px;color:#475569;">{wins}</td></tr>'
                )
            direction = _esc((competitor_pulse.get("sector_trend") or {}).get("direction") or "stable")
            competitor_section = f"""
            <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">Competitor pulse — {_esc(competitor_pulse.get('sector') or 'your sector')}</h3>
            <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eef2f7;border-radius:8px;overflow:hidden;">
              <tr style="background:#f8fafc;"><th style="text-align:left;padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Top suppliers</th><th style="text-align:right;padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Wins</th></tr>
              {sup_rows}
            </table>
            <p style="font-size:12px;color:#64748b;margin:6px 0 0;">Sector award activity is <strong>{direction}</strong>
              over the last {competitor_pulse.get('period_days', 90)} days ({competitor_pulse.get('total_awards', 0)} awards analysed).</p>
            """

        # 2f. Offline evidence artefacts — attach the exportable PDFs directly to
        # the digest (audit: vendors need these in the inbox, not only on the
        # dashboard). Badge Certificate + Priority Placement + Bid-Timing are
        # generic; Competitor Activity needs a specific tender number so it stays
        # on-demand (dashboard + endpoint). Each is best-effort — one failure
        # must never block the email. Reuses the same builders as the endpoints.
        digest_attachments: list[tuple[str, bytes]] = []
        if user:
            from app.services import vendor_artifacts_builder as _vab
            for _builder in (_vab.build_badge_certificate, _vab.build_priority_placement, _vab.build_bid_timing):
                try:
                    fn, pdf_bytes = _builder(db, user, company_override=override_company)
                    if pdf_bytes:
                        digest_attachments.append((fn, pdf_bytes))
                except Exception as art_err:
                    logger.warning("[VendorDigest] artefact %s failed for %s: %s", getattr(_builder, "__name__", "?"), vendor_id, art_err)
            # Attach the status snapshot too (was link-only) when it was generated.
            if snapshot_pdf:
                digest_attachments.append((f"BOOPPA-Status-Snapshot-{vendor_id}.pdf", snapshot_pdf))

            # Vendor Pro premium attachments: the consolidated monthly intelligence
            # report (flagship), the sector competitor signals report, and the PDPA
            # drift report. Each best-effort — never blocks the digest.
            if is_pro:
                _safe_co = (company or "report").replace("/", "-").replace(" ", "-")
                try:
                    from app.services.vendor_pro_report_generator import build_pro_report_pdf
                    rep = build_pro_report_pdf(
                        db, vendor_id, company=company, plan_label=plan_label,
                        trust_score=getattr(score_record, "total_score", None),
                        compliance_score=getattr(score_record, "compliance_score", None),
                        profile_views_30d=profile_views,
                    )
                    if rep:
                        digest_attachments.append((f"BOOPPA-Vendor-Pro-Report-{_safe_co}.pdf", rep))
                except Exception as rep_err:
                    logger.warning("[VendorDigest] pro report failed for %s: %s", vendor_id, rep_err)
                if competitor_pulse:
                    try:
                        from app.services.competitor_signals_generator import generate_competitor_signals_pdf
                        cs = generate_competitor_signals_pdf(competitor_pulse, company)
                        if cs:
                            digest_attachments.append((f"BOOPPA-Competitor-Signals-{_safe_co}.pdf", cs))
                    except Exception as cs_err:
                        logger.warning("[VendorDigest] competitor signals PDF failed for %s: %s", vendor_id, cs_err)
        _pro_attach_note = (
            ' Your <strong>Vendor Pro Monthly Intelligence Report</strong> (consolidated) and '
            'Competitor Signals report are attached too. (The full PDPA Monitor Report arrives via a separate email '
            'when the deep scan completes.)'
            if is_pro else ""
        )
        attachments_note = (
            '<p style="font-size:13px;color:#334155;margin:16px 0 0;">📎 <strong>Attached to this email:</strong> '
            'your offline evidence PDFs — Badge Certificate, Priority Placement Report, and Bid-Timing Report. '
            'File them, forward them, or attach them to a tender.' + _pro_attach_note +
            ' <a href="https://www.booppa.io/vendor/dashboard" style="color:#10b981;">Competitor Activity reports</a> '
            'are available on demand from your dashboard.</p>'
            if digest_attachments else ""
        )

        # 3. Single consolidated digest / welcome email
        if is_first_cycle:
            subject = f"Welcome to {plan_label} — here's everything included"
            header = f"Welcome to {plan_label}"
            intro = f"Your <strong>{plan_label}</strong> subscription is now active. Here's everything it unlocks:"
        else:
            subject = f"Your monthly {plan_label} digest — {company}"
            header = f"Monthly {plan_label} Digest"
            intro = "Here is your BOOPPA profile activity and tender intelligence for the past 30 days:"

        # Trend deltas vs last cycle + sector standing — the "is it improving?"
        # signal that makes the scores meaningful.
        def _delta_html(d) -> str:
            if d is None:
                return ""
            if d > 0:
                return f' <span style="color:#10b981;font-size:12px;font-weight:bold;">▲ {d}</span>'
            if d < 0:
                return f' <span style="color:#ef4444;font-size:12px;font-weight:bold;">▼ {abs(d)}</span>'
            return ' <span style="color:#94a3b8;font-size:12px;">no change</span>'

        trust_delta = _delta_html(trend.get("total_delta")) if trend else ""
        comp_delta = _delta_html(trend.get("compliance_delta")) if trend else ""
        benchmark_line = (
            f'<p style="margin:10px 0 0;font-size:13px;color:#334155;">📊 <strong>Sector standing:</strong> '
            f'top {max(1, 100 - benchmark["percentile"])}% in {_esc(benchmark["sector"])} '
            f'— ahead of {benchmark["percentile"]}% of peers.</p>'
            if benchmark else ""
        )
        scores_box = f"""
                <div style="background:#f8fafc;border-radius:8px;padding:20px;margin:20px 0;">
                  <p style="margin:4px 0;"><strong>Trust Score:</strong> {score_record.total_score}/100{trust_delta}</p>
                  <p style="margin:4px 0;"><strong>Compliance Score:</strong> {score_record.compliance_score}/100{comp_delta}</p>
                  <p style="margin:4px 0;"><strong>Profile Views (30d):</strong> {profile_views}</p>
                  {benchmark_line}
                </div>"""

        body_inner = f"""
                <p>Hello <strong>{company}</strong>,</p>
                <p>{intro}</p>
                {scores_box}
                {snapshot_cta}
                {attachments_note}
                {pro_note}
                {gebiz_section}
                {competitor_section}
                {features_section}
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    View Full Dashboard →
                  </a>
                </p>"""

        from app.services.email_layout import branded_email_html
        email_svc = EmailService()
        asyncio.run(email_svc.send_html_email(
            to_email=vendor_email,
            subject=subject,
            body_html=branded_email_html(body_inner, title=header,
                                         preheader=f"{plan_label} · Trust {score_record.total_score}/100"),
            attachments=digest_attachments or None,
        ))
        logger.info(f"Vendor digest ({plan_label}, first_cycle={is_first_cycle}) sent for vendor {vendor_id}")
    except Exception as exc:
        logger.error(f"Vendor Active health check failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


def _buyer_tier_from_product(product_type: str | None) -> tuple[str, str]:
    """(tier, plan_label) from a buyer subscription product_type. Defaults to Starter."""
    p = (product_type or "").lower()
    if p.startswith("buyer_enterprise"):
        return "enterprise", "Buyer Enterprise"
    if p.startswith("buyer_pro"):
        return "pro", "Buyer Professional"
    return "starter", "Buyer Essentials"


@celery_app.task(bind=True, max_retries=2, name="buyer_procurement_digest_task")
def buyer_procurement_digest_task(
    self,
    user_id: str,
    user_email: str,
    product_type: str | None = None,
    override_company: str | None = None,
    is_first_cycle: bool = False,
    demo: bool = False,
):
    """
    Single consolidated Procurement Intelligence Digest for buyer subscribers.

    The buyer analog of `vendor_active_health_check_task`: the ONE email a buyer
    subscription sends per cycle (the bare "Activated" email is suppressed for
    buyers in `_activate_subscription`). It combines the two recurring buyer
    assets into one deliverable:
      • Watched-supplier drift — each supplier on the org watchlist with its
        current Trust/Compliance score, risk signal, and month-over-month delta.
      • New GeBIZ tenders to evaluate (soonest-closing open tenders).

    Tiered by plan (resolved from `product_type`, not hardcoded SKUs):
      • Starter    → email summary only, no attachment.
      • Pro        → + attached Procurement Intelligence Report PDF.
      • Enterprise → + attached PDF with full multi-supplier watchlist section.

    `is_first_cycle=True` (set by the activation wrapper) switches the framing
    from "monthly digest" to "welcome". `override_company` is test-harness-only
    and never mutates the stored profile.
    """
    db = SessionLocal()
    try:
        from app.core.models import User
        from app.services.email_service import EmailService
        from app.services.email_layout import branded_email_html
        from app.services.buyer_procurement_insights import (
            get_watched_suppliers_with_status, summarise_watchlist,
        )
        from app.services.vendor_active_insights import get_tender_matches

        tier, plan_label = _buyer_tier_from_product(product_type)

        # Single window/date for the whole deliverable: the email header, the
        # subject and the attached PDF all reference THIS date, so the digest
        # can't show one "as of" date on the email and another on the PDF.
        digest_date = datetime.now(timezone.utc).strftime("%d %B %Y")

        user = UserRepository.get_by_id(db, str(user_id))
        company = (override_company or "").strip() or (getattr(user, "company", None) or "Your organisation")

        def _esc(s) -> str:
            return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # 1. Watchlist drift — the substance of the digest. Best-effort.
        suppliers = get_watched_suppliers_with_status(db, user_id)
        summary = summarise_watchlist(suppliers)

        # 2. New tenders to evaluate. Buyers have no VendorSector, so matches come
        #    back unclassified — still a useful closing-soon list.
        tender_matches = get_tender_matches(db, user_id, limit=8)

        # ── Watchlist section ──────────────────────────────────────────────────
        def _delta_html(d) -> str:
            if d is None or not isinstance(d, int):
                return ""
            if d > 0:
                return f' <span style="color:#10b981;font-size:12px;font-weight:bold;">▲ {d}</span>'
            if d < 0:
                return f' <span style="color:#ef4444;font-size:12px;font-weight:bold;">▼ {abs(d)}</span>'
            return ' <span style="color:#94a3b8;font-size:12px;">no change</span>'

        watchlist_section = ""
        if suppliers:
            rows = ""
            for sup in suppliers[:15]:
                name = _esc((sup.get("vendor_name") or "")[:48])
                if sup.get("resolved"):
                    status = sup.get("risk_signal") or sup.get("procurement_readiness") or "MONITORED"
                    trust = sup.get("trust_score")
                    trust_txt = f'{trust}{_delta_html(sup.get("trust_delta"))}' if trust is not None else "—"
                else:
                    status = "UNRATED"
                    trust_txt = "—"
                rows += (
                    f'<tr><td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:13px;">{name}</td>'
                    f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:12px;color:#475569;white-space:nowrap;">{_esc(status)}</td>'
                    f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:13px;text-align:right;white-space:nowrap;">{trust_txt}</td></tr>'
                )
            watchlist_section = f"""
            <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">Your watched suppliers</h3>
            <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eef2f7;border-radius:8px;overflow:hidden;">
              <tr style="background:#f8fafc;"><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Supplier</th><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Status</th><th style="text-align:right;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Trust</th></tr>
              {rows}
            </table>
            <p style="font-size:12px;color:#64748b;margin:6px 0 0;">Δ is vs each supplier's previous scan. UNRATED suppliers aren't a claimed profile yet — scores populate once they verify.</p>
            """
        else:
            watchlist_section = """
            <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">Your watched suppliers</h3>
            <p style="font-size:13px;color:#334155;">You aren't watching any suppliers yet.
              <a href="https://www.booppa.io/buyer/dashboard" style="color:#0ea5e9;">Add suppliers to your watchlist</a>
              to start monthly monitoring of their Trust &amp; PDPA scores and risk signals.</p>
            """

        # ── Tender section ─────────────────────────────────────────────────────
        gebiz_section = ""
        if tender_matches:
            trows = ""
            for t in tender_matches[:8]:
                cd = t.get("closing_date")
                close = cd.strftime("%d %b %Y") if cd else "—"
                title = _esc((t.get("title") or "")[:90])
                url = t.get("url")
                cell = f'<a href="{_esc(url)}" style="color:#0ea5e9;text-decoration:none;">{title}</a>' if url else title
                agency = _esc((t.get("agency") or "")[:28])
                trows += (
                    f'<tr><td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:13px;">{cell}</td>'
                    f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:12px;color:#475569;">{agency}</td>'
                    f'<td style="padding:7px 8px;border-bottom:1px solid #eef2f7;font-size:12px;color:#475569;white-space:nowrap;">{close}</td></tr>'
                )
            gebiz_section = f"""
            <h3 style="color:#0f172a;font-size:15px;margin:24px 0 8px;">New tenders to evaluate</h3>
            <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eef2f7;border-radius:8px;overflow:hidden;">
              <tr style="background:#f8fafc;"><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Tender</th><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Agency</th><th style="text-align:left;padding:7px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;">Closes</th></tr>
              {trows}
            </table>
            <p style="font-size:12px;color:#64748b;margin:6px 0 0;">
              <a href="https://www.booppa.io/buyer/tenders" style="color:#0ea5e9;">See all open tenders →</a>
            </p>
            """

        # ── Headline summary box ───────────────────────────────────────────────
        alerting_line = ""
        if summary.get("alerting"):
            names = ", ".join(_esc(n) for n in (summary.get("alerting_names") or []))
            alerting_line = (
                f'<p style="margin:10px 0 0;font-size:13px;color:#b91c1c;">⚠ <strong>{summary["alerting"]}</strong> '
                f'supplier(s) need attention this cycle: {names}.</p>'
            )
        summary_box = f"""
        <div style="background:#f8fafc;border-radius:8px;padding:20px;margin:20px 0;">
          <p style="margin:4px 0;"><strong>Suppliers watched:</strong> {summary.get('total', 0)}</p>
          <p style="margin:4px 0;"><strong>Need attention:</strong> {summary.get('alerting', 0)}</p>
          <p style="margin:4px 0;"><strong>Score slipped (30d):</strong> {summary.get('slipped', 0)}</p>
          {alerting_line}
        </div>"""

        # ── PDF attachments ────────────────────────────────────────────────────
        digest_attachments: list[tuple[str, bytes]] = []
        _safe_co = (company or "report").replace("/", "-").replace(" ", "-")

        # Welcome pack — every tier, first cycle only. A static onboarding artifact
        # that describes what the plan includes and how to use each capability, so
        # even Starter (Buyer Essentials) buyers get a PDF deliverable on checkout.
        welcome_attached = False
        if is_first_cycle:
            try:
                from app.services.buyer_essentials_pack_generator import generate_buyer_essentials_pack
                welcome_pdf = generate_buyer_essentials_pack({
                    "company": company,
                    "buyer_email": user_email,
                    "plan_label": plan_label,
                })
                if welcome_pdf:
                    digest_attachments.append((f"BOOPPA-Welcome-Pack-{_safe_co}.pdf", welcome_pdf))
                    welcome_attached = True
            except Exception as wp_err:
                logger.warning("[BuyerDigest] welcome pack PDF failed for %s: %s", user_id, wp_err)

        # Procurement Intelligence Report — every tier (Starter included). The
        # renderer tailors the "what your plan includes" copy per tier, so the
        # Starter report stays honest about its scope. `demo=demo` populates the
        # comparison table with the SG sample estate on test-mode checkouts.
        report_attached = False
        try:
            from app.services.buyer_procurement_report_generator import build_buyer_procurement_report_pdf
            pdf = build_buyer_procurement_report_pdf(
                db, user_id, tier=tier, company=company, plan_label=plan_label, demo=demo,
                generated_at=digest_date,
            )
            if pdf:
                digest_attachments.append((f"BOOPPA-Procurement-Report-{_safe_co}.pdf", pdf))
                report_attached = True
        except Exception as rep_err:
            logger.warning("[BuyerDigest] report PDF failed for %s: %s", user_id, rep_err)

        _attached_labels = []
        if welcome_attached:
            _attached_labels.append("your Welcome Pack (PDF)")
        if report_attached:
            _attached_labels.append("your Procurement Intelligence Report (PDF)")
        attachments_note = (
            '<p style="font-size:13px;color:#334155;margin:16px 0 0;">📎 <strong>Attached:</strong> '
            f'{" and ".join(_attached_labels)} — file it, forward it, or take it into a review.</p>'
            if _attached_labels else ""
        )

        # ── Email framing ──────────────────────────────────────────────────────
        if is_first_cycle:
            subject = f"Welcome to {plan_label} — your procurement intelligence starts now"
            header = f"Welcome to {plan_label}"
            intro = f"Your <strong>{plan_label}</strong> subscription is now active. Here's your procurement intelligence:"
        else:
            subject = f"Your monthly Procurement Intelligence Digest — {company}"
            header = "Monthly Procurement Intelligence Digest"
            intro = (
                f"Here is your supplier monitoring and tender intelligence "
                f"<strong>as of {digest_date}</strong>:"
            )

        # Demo/test-checkout preview (Stripe livemode=false): tag the subject so the
        # recipient can tell a fire-all sample from a real cycle deliverable.
        if demo:
            subject = f"[DEMO] {subject}"

        body_inner = f"""
                <p>Hello <strong>{_esc(company)}</strong>,</p>
                <p>{intro}</p>
                {summary_box}
                {attachments_note}
                {watchlist_section}
                {gebiz_section}
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/buyer/dashboard"
                     style="background:#0f172a;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    Open Procurement Dashboard →
                  </a>
                </p>"""

        email_svc = EmailService()
        ok = asyncio.run(email_svc.send_html_email(
            to_email=user_email,
            subject=subject,
            body_html=branded_email_html(
                body_inner, title=header,
                preheader=f"{plan_label} · {summary.get('total', 0)} suppliers watched",
            ),
            attachments=digest_attachments or None,
        ))
        if not ok:
            logger.error(
                "[BuyerDigest] email delivery rejected for user=%s tier=%s first_cycle=%s",
                user_id, tier, is_first_cycle,
            )
        else:
            logger.info(
                "[BuyerDigest] digest (%s, first_cycle=%s) sent for buyer %s",
                plan_label, is_first_cycle, user_id,
            )
    except Exception as exc:
        logger.error(f"Buyer procurement digest failed for {user_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="buyer_supplier_snapshot_task")
def buyer_supplier_snapshot_task(
    self,
    buyer_user_id: str,
    buyer_email: str,
    vendor_ref: str,
    vendor_name: str | None = None,
    notes: str | None = None,
    product_type: str | None = None,
    *,
    is_certificate: bool = False,
    demo: bool = False,
):
    """Instant supplier snapshot (#3) / anchored Due-Diligence Certificate (#2).

    Fired the moment a buyer adds a supplier to their watchlist, and reusable as an
    on-demand certificate. Tiered like the digest:

      * Starter          → un-anchored verification snapshot PDF + HTML card.
      * Pro / Enterprise → anchored Due-Diligence Certificate (SHA-256 on Polygon),
        attached to the email.

    In demo/test-checkout mode (`demo=True`) the certificate uses a deterministic
    mock tx hash instead of hitting the chain — no gas, instant, still shows the
    buyer what the real artifact looks like.
    """
    db = SessionLocal()
    try:
        from app.services.supplier_due_diligence_generator import (
            build_certificate_data, generate_certificate_pdf,
            evidence_hash_for, demo_tx_hash, _xml_escape,
            SUPPLIER_DUE_DILIGENCE_SCHEMA_VERSION,
        )
        from app.services.email_layout import branded_email_html

        tier, plan_label = _buyer_tier_from_product(product_type)
        # Pro/Enterprise get the anchored certificate; Starter gets a plain snapshot.
        wants_cert = is_certificate or tier in ("pro", "enterprise")

        data = build_certificate_data(
            db, buyer_user_id, vendor_ref,
            vendor_name=vendor_name, notes=notes, is_certificate=wants_cert,
        )
        supplier_name = data.get("supplier_name") or vendor_ref

        # Render once un-anchored to get the fingerprint, then (for a real cert)
        # anchor that hash and re-render with the tx reference embedded.
        pdf = generate_certificate_pdf(data)
        tx_hash = None
        anchored = False
        if wants_cert:
            ev_hash = evidence_hash_for(pdf)
            if demo:
                tx_hash = demo_tx_hash(ev_hash)
                anchored = False  # demo: reference only, not on-chain
            else:
                try:
                    from app.services.blockchain import BlockchainService
                    tx_hash = asyncio.run(BlockchainService().anchor_evidence(
                        ev_hash,
                        metadata=f"supplier-due-diligence:{supplier_name}",
                    ))
                    anchored = bool(tx_hash)
                except Exception as anc_err:
                    logger.warning(
                        "[DueDiligence] anchor failed for buyer=%s ref=%s: %s",
                        buyer_user_id, vendor_ref, anc_err,
                    )
                # A real (non-demo) certificate promises an on-chain anchor. If the
                # anchor did not land, do NOT ship an unanchored cert as if it were
                # verified — alert and retry so a transient RPC/gas blip self-heals,
                # and the buyer never receives a certificate we can't stand behind.
                if not anchored:
                    try:
                        from app.services.fulfillment import alert_payment_fulfillment_issue
                        asyncio.run(alert_payment_fulfillment_issue(
                            reason=(
                                f"Supplier due-diligence certificate anchor failed for "
                                f"buyer={buyer_user_id} ref={vendor_ref} "
                                f"supplier={supplier_name} — cert withheld pending anchor."
                            ),
                            product_type=None,
                            customer_email=None,
                            notify_customer=False,
                        ))
                    except Exception as alert_err:
                        logger.error("[DueDiligence] alert failed: %s", alert_err)
                    raise self.retry(
                        exc=RuntimeError("due-diligence anchor failed; cert withheld"),
                        countdown=300,
                    )
            data["tx_hash"] = tx_hash
            data["anchored"] = anchored
            pdf = generate_certificate_pdf(data)

        # ── Email ───────────────────────────────────────────────────────────────
        doc_label = "Due-Diligence Certificate" if wants_cert else "Verification Snapshot"
        demo_tag = "[DEMO] " if demo else ""
        _safe = _xml_escape(supplier_name)
        _safe_file = (supplier_name or "supplier").replace("/", "-").replace(" ", "-")

        if wants_cert and tx_hash:
            verify_line = (
                f'<p style="font-size:13px;color:#334155;">📎 <strong>Attached:</strong> an '
                f'anchored Due-Diligence Certificate for your audit file. Its fingerprint is '
                f'{"recorded on Polygon" if anchored else "referenced for on-chain anchoring"} — '
                f'transaction <code style="font-size:12px;">{_xml_escape(tx_hash)[:22]}…</code>.</p>'
            )
        else:
            verify_line = (
                '<p style="font-size:13px;color:#334155;">📎 <strong>Attached:</strong> a '
                'verification snapshot of this supplier\'s current state. Anchored, independently '
                'verifiable certificates are included on Pro and Enterprise plans.</p>'
            )

        status = (data.get("risk_signal") or data.get("procurement_readiness")
                  or ("MONITORED" if data.get("resolved") else "UNRATED"))
        header = f"You're now monitoring {supplier_name}"
        body_inner = f"""
                <p>You added <strong>{_safe}</strong> to your watchlist. Here's their verified
                   state right now, captured as a {doc_label.lower()} you can keep on file.</p>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                            padding:16px 20px;margin:16px 0;">
                  <p style="margin:4px 0;"><strong>Supplier:</strong> {_safe}</p>
                  <p style="margin:4px 0;"><strong>Verification status:</strong> {_xml_escape(status)}</p>
                  <p style="margin:4px 0;"><strong>Trust score:</strong> {data.get('trust_score') if data.get('trust_score') is not None else '—'}
                     &nbsp;·&nbsp; <strong>PDPA / Compliance:</strong> {data.get('compliance_score') if data.get('compliance_score') is not None else '—'}</p>
                </div>
                {verify_line}
                <p>We'll email you immediately if this supplier's score drops or their risk
                   signal changes — you don't have to check back.</p>
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/buyer/dashboard"
                     style="background:#0f172a;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    Open Procurement Dashboard →
                  </a>
                </p>"""

        email_svc = EmailService()
        ok = asyncio.run(email_svc.send_html_email(
            to_email=buyer_email,
            subject=f"{demo_tag}Supplier {doc_label}: {supplier_name}",
            category="marketing",
            body_html=branded_email_html(
                body_inner, title=f"{demo_tag}{header}",
                preheader=f"{plan_label} · {supplier_name} verified state on file",
            ),
            attachments=[(f"BOOPPA-Supplier-{doc_label.replace(' ', '-')}-{_safe_file}.pdf", pdf)],
        ))
        if not ok:
            logger.error(
                "[DueDiligence] email delivery rejected for buyer=%s ref=%s tier=%s",
                buyer_user_id, vendor_ref, tier,
            )
        else:
            logger.info(
                "[DueDiligence] %s (schema v%s, demo=%s) sent for buyer=%s supplier=%s",
                doc_label, SUPPLIER_DUE_DILIGENCE_SCHEMA_VERSION, demo, buyer_user_id, supplier_name,
            )
    except Retry:
        # Deliberate retry (e.g. anchor withheld above) — propagate untouched.
        raise
    except Exception as exc:
        logger.error(f"Supplier snapshot/certificate failed for buyer {buyer_user_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="buyer_supplier_drift_alert_task")
def buyer_supplier_drift_alert_task(
    self,
    buyer_user_id: str,
    buyer_email: str,
    vendor_ref: str,
    vendor_name: str,
    reason: str,
    headline: str,
    detail_html: str,
    product_type: str | None = None,
    *,
    demo: bool = False,
):
    """Event-triggered supplier drift alert (#1).

    Sent the moment a watched supplier's state changes materially — score drop,
    flip into FLAGGED/CRITICAL, or an approaching certificate expiry. Tiered like
    every other buyer deliverable:

      * Starter          → + an attached un-anchored verification snapshot PDF
        capturing the supplier's new state (no gas / no on-chain anchor).
      * Pro / Enterprise → + an attached anchored Due-Diligence Certificate
        capturing the supplier's new verified state for the buyer's audit file.

    Either way the alert email now carries a PDF for every tier. In demo mode the
    certificate uses a mock tx hash (no gas). Dedup / threshold logic lives in the
    sweep that enqueues this task; here we just render + send.
    """
    db = SessionLocal()
    try:
        from app.services.email_layout import branded_email_html
        from app.services.supplier_due_diligence_generator import (
            build_certificate_data, generate_certificate_pdf,
            evidence_hash_for, demo_tx_hash, _xml_escape,
        )

        tier, plan_label = _buyer_tier_from_product(product_type)
        wants_cert = tier in ("pro", "enterprise")

        # Every tier gets a PDF: an anchored certificate for Pro/Enterprise, or an
        # un-anchored verification snapshot for Starter.
        pdf = None
        tx_hash = None
        if wants_cert:
            data = build_certificate_data(
                db, buyer_user_id, vendor_ref,
                vendor_name=vendor_name, is_certificate=True,
            )
            pdf = generate_certificate_pdf(data)
            ev_hash = evidence_hash_for(pdf)
            anchored = False
            if demo:
                tx_hash = demo_tx_hash(ev_hash)
            else:
                try:
                    from app.services.blockchain import BlockchainService
                    tx_hash = asyncio.run(BlockchainService().anchor_evidence(
                        ev_hash, metadata=f"supplier-drift:{vendor_name}",
                    ))
                    anchored = bool(tx_hash)
                except Exception as anc_err:
                    logger.warning(
                        "[DriftAlert] anchor failed for buyer=%s ref=%s: %s",
                        buyer_user_id, vendor_ref, anc_err,
                    )
            data["tx_hash"] = tx_hash
            data["anchored"] = anchored
            pdf = generate_certificate_pdf(data)
        else:
            # Starter — un-anchored snapshot (is_certificate=False, no tx_hash).
            data = build_certificate_data(
                db, buyer_user_id, vendor_ref,
                vendor_name=vendor_name, is_certificate=False,
            )
            pdf = generate_certificate_pdf(data)

        demo_tag = "[DEMO] " if demo else ""
        _safe = _xml_escape(vendor_name)
        _safe_file = (vendor_name or "supplier").replace("/", "-").replace(" ", "-")

        if wants_cert:
            attach_line = (
                '<p style="font-size:13px;color:#334155;">📎 <strong>Attached:</strong> an '
                'anchored Due-Diligence Certificate capturing this supplier\'s new verified '
                'state — drop it straight into your audit file.</p>'
            )
        else:
            attach_line = (
                '<p style="font-size:13px;color:#334155;">📎 <strong>Attached:</strong> a '
                'verification snapshot capturing this supplier\'s new state. Anchored, '
                'audit-grade Due-Diligence Certificates are included on Pro and Enterprise plans.</p>'
            )

        body_inner = f"""
                <p>A supplier you're monitoring just changed. We caught it as it happened
                   so you don't have to wait for your monthly digest.</p>
                <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;
                            padding:16px 20px;margin:16px 0;">
                  <p style="margin:4px 0;font-size:15px;"><strong>{_safe}</strong></p>
                  <p style="margin:4px 0;color:#b91c1c;"><strong>{_xml_escape(headline)}</strong></p>
                  <p style="margin:8px 0 0;color:#334155;">{detail_html}</p>
                </div>
                {attach_line}
                <p style="margin-top:24px;">
                  <a href="https://www.booppa.io/buyer/dashboard"
                     style="background:#0f172a;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    Review supplier →
                  </a>
                </p>"""

        attachments = None
        if pdf:
            _pdf_name = (
                f"BOOPPA-Supplier-Due-Diligence-Certificate-{_safe_file}.pdf"
                if wants_cert else
                f"BOOPPA-Supplier-Verification-Snapshot-{_safe_file}.pdf"
            )
            attachments = [(_pdf_name, pdf)]

        email_svc = EmailService()
        ok = asyncio.run(email_svc.send_html_email(
            to_email=buyer_email,
            subject=f"{demo_tag}Supplier alert: {vendor_name} — {headline}",
            category="marketing",
            body_html=branded_email_html(
                body_inner,
                title=f"{demo_tag}Supplier alert: {vendor_name}",
                preheader=f"{plan_label} · {headline}",
            ),
            attachments=attachments,
        ))
        if not ok:
            logger.error(
                "[DriftAlert] email delivery rejected for buyer=%s ref=%s reason=%s",
                buyer_user_id, vendor_ref, reason,
            )
        else:
            logger.info(
                "[DriftAlert] %s alert (demo=%s, cert=%s) sent for buyer=%s supplier=%s",
                reason, demo, wants_cert, buyer_user_id, vendor_name,
            )
    except Exception as exc:
        logger.error(f"Supplier drift alert failed for buyer {buyer_user_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(name="buyer_supplier_drift_sweep_task")
def buyer_supplier_drift_sweep_task(demo: bool = False):
    """Event-triggered supplier drift sweep (#1).

    Walks every buyer org's watchlist, resolves each supplier's live status, and
    compares it against the per-(buyer, supplier) `BuyerSupplierAlert` ledger. When
    a material change crosses a threshold — score drop, flip to FLAGGED/CRITICAL,
    or an approaching cert expiry — it enqueues a tiered drift alert to the org
    owner and updates the ledger so the same change never re-fires.

    Runs on a beat schedule as the reliable backstop for the whole watched estate
    (independent of whether a supplier holds an active vendor subscription). The
    ledger dedup means firing this frequently is cheap and idempotent.
    """
    db = SessionLocal()
    enqueued = 0
    try:
        from datetime import datetime
        from app.core.models import User
        from app.core.models import Organisation, VendorWatchlistItem
        from app.core.models import BuyerSupplierAlert
        from app.services.buyer_procurement_insights import (
            _resolve_watchlist_vendor_user, _supplier_status,
            get_supplier_cert_expiry, evaluate_supplier_drift,
        )

        orgs = db.query(Organisation).all()
        for org in orgs:
            owner = db.query(User).filter(User.id == org.owner_user_id).first()
            if not owner or not owner.email:
                continue
            plan = (getattr(owner, "plan", "") or "").lower().strip()
            items = (
                db.query(VendorWatchlistItem)
                .filter(VendorWatchlistItem.organisation_id == org.id)
                .all()
            )
            seen_refs: set = set()
            for it in items:
                if it.vendor_ref in seen_refs:
                    continue
                seen_refs.add(it.vendor_ref)

                vuid = _resolve_watchlist_vendor_user(db, it.vendor_ref)
                if not vuid:
                    continue  # unresolved suppliers have no live status to drift
                current = _supplier_status(db, vuid)
                cert_expiry = get_supplier_cert_expiry(db, vuid)

                ledger = (
                    db.query(BuyerSupplierAlert)
                    .filter(
                        BuyerSupplierAlert.buyer_user_id == owner.id,
                        BuyerSupplierAlert.vendor_ref == it.vendor_ref,
                    )
                    .first()
                )
                alert = evaluate_supplier_drift(current, cert_expiry, ledger)
                if not alert:
                    continue

                # Upsert the ledger to the new baseline so this change won't re-fire.
                if ledger is None:
                    ledger = BuyerSupplierAlert(
                        buyer_user_id=owner.id, vendor_ref=it.vendor_ref,
                    )
                    db.add(ledger)
                ledger.last_trust_score = current.get("trust_score")
                ledger.last_risk_signal = current.get("risk_signal")
                if alert.get("reason") == "cert_expiry" and alert.get("expiry") is not None:
                    ledger.last_expiry_warned_for = alert["expiry"]
                ledger.last_reason = alert.get("reason")
                ledger.last_alerted_at = datetime.utcnow()
                db.commit()

                buyer_supplier_drift_alert_task.delay(
                    str(owner.id), owner.email, it.vendor_ref,
                    it.vendor_name or it.vendor_ref,
                    alert.get("reason"), alert.get("headline", "Supplier status changed"),
                    alert.get("detail", ""), product_type=plan, demo=demo,
                )
                enqueued += 1

        logger.info("[DriftSweep] evaluated %d orgs, enqueued %d alerts (demo=%s)",
                    len(orgs), enqueued, demo)
        return {"orgs": len(orgs), "alerts": enqueued}
    except Exception as exc:
        logger.error(f"Buyer supplier drift sweep failed: {exc}")
        db.rollback()
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="buyer_tender_fit_push_task")
def buyer_tender_fit_push_task(
    self,
    buyer_user_id: str,
    buyer_email: str,
    tender_no: str,
    tender_title: str,
    tender_agency: str | None,
    tender_url: str | None,
    closing_label: str | None,
    sector: str,
    matched_names: list | None = None,
    product_type: str | None = None,
    *,
    demo: bool = False,
):
    """Per-tender high-fit push (#4).

    A single buyer-framed email sent the moment a strongly-matching GeBIZ tender is
    ingested: a tender in a sector the buyer already sources from (one or more of
    their watched suppliers operate there). Unlike the vendor tender alert ("worth
    bidding on"), the buyer framing is evaluate/shortlist/publish — the buyer is on
    the procuring side. Carries a one-page Tender Opportunity Brief PDF for every
    tier; dedup lives in the sweep.
    """
    db = SessionLocal()
    try:
        from app.core.models import User
        from app.services.email_service import EmailService
        from app.services.email_layout import branded_email_html
        from app.services.supplier_due_diligence_generator import _xml_escape

        tier, plan_label = _buyer_tier_from_product(product_type)

        title = _xml_escape((tender_title or "").strip()[:140] or "New government tender")
        agency = _xml_escape((tender_agency or "").strip()[:80] or "Government Agency")
        sector_txt = _xml_escape((sector or "").strip() or "your procurement area")
        closes = _xml_escape((closing_label or "").strip() or "—")
        url = (tender_url or "").strip()

        names = [str(n).strip() for n in (matched_names or []) if str(n).strip()]
        if names:
            shown = ", ".join(_xml_escape(n[:48]) for n in names[:4])
            extra = f" and {len(names) - 4} more" if len(names) > 4 else ""
            match_line = (
                f'<p style="margin:10px 0 0;font-size:13px;color:#334155;">This tender is in '
                f'<strong>{sector_txt}</strong> — a sector your watched supplier(s) '
                f'<strong>{shown}{extra}</strong> operate in, so you likely have vetted '
                f'suppliers ready to invite or benchmark.</p>'
            )
        else:
            match_line = (
                f'<p style="margin:10px 0 0;font-size:13px;color:#334155;">This tender is in '
                f'<strong>{sector_txt}</strong>, a sector on your procurement radar.</p>'
            )

        cta = (
            f'<p style="margin:20px 0 0;">'
            f'<a href="{_xml_escape(url) if url else "https://www.booppa.io/buyer/tenders"}" '
            f'style="background:#0f172a;color:#fff;padding:12px 24px;text-decoration:none;'
            f'border-radius:8px;font-weight:bold;display:inline-block;">View tender →</a></p>'
        )

        # One-page Tender Opportunity Brief PDF — attached for every tier so the
        # push is a filable deliverable, not just an email.
        brief_pdf = None
        try:
            from app.services.tender_brief_generator import generate_tender_brief_pdf
            _company = None
            _u = UserRepository.get_by_id(db, str(buyer_user_id))
            if _u:
                _company = getattr(_u, "company_name", None) or getattr(_u, "full_name", None)
            brief_pdf = generate_tender_brief_pdf({
                "company_name": _company,
                "plan_label": plan_label,
                "tender_no": tender_no,
                "tender_title": tender_title,
                "tender_agency": tender_agency,
                "tender_url": tender_url,
                "closing_label": closing_label,
                "sector": sector,
                "matched_names": names,
            })
        except Exception as brief_err:
            logger.warning(
                "[TenderPush] brief PDF failed buyer=%s tender=%s: %s",
                buyer_user_id, tender_no, brief_err,
            )

        demo_tag = "[DEMO] " if demo else ""
        subject = f"{demo_tag}High-fit tender for {sector_txt}: {title[:70]}"

        body_inner = f"""
                <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:20px;margin:0 0 8px;">
                  <p style="margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#047857;font-weight:bold;">High-fit tender just opened</p>
                  <p style="margin:8px 0 0;font-size:16px;color:#064e3b;font-weight:bold;">{title}</p>
                  <p style="margin:6px 0 0;font-size:13px;color:#065f46;">{agency} · closes {closes}</p>
                </div>
                {match_line}
                {'<p style="font-size:13px;color:#334155;margin:14px 0 0;">📎 <strong>Attached:</strong> a one-page Tender Opportunity Brief — file it, forward it to your procurement lead, or take it into an evaluation.</p>' if brief_pdf else ''}
                {cta}
                <p style="margin:24px 0 0;font-size:12px;color:#64748b;">
                  You're receiving this because a newly-published tender matched a sector your
                  watched suppliers operate in. It also appears in your monthly Procurement
                  Intelligence Digest — this is the early heads-up.
                </p>"""

        _safe_tno = (tender_no or "tender").replace("/", "-").replace(" ", "-")
        attachments = [(f"BOOPPA-Tender-Opportunity-Brief-{_safe_tno}.pdf", brief_pdf)] if brief_pdf else None
        email_svc = EmailService()
        ok = asyncio.run(email_svc.send_html_email(
            to_email=buyer_email,
            subject=subject,
            category="marketing",
            body_html=branded_email_html(
                body_inner, title="High-fit tender alert",
                preheader=f"{sector_txt} · {agency}",
            ),
            attachments=attachments,
        ))
        if not ok:
            logger.error(
                "[TenderPush] delivery rejected buyer=%s tender=%s demo=%s",
                buyer_user_id, tender_no, demo,
            )
        else:
            logger.info(
                "[TenderPush] pushed tender=%s to buyer=%s (%s, demo=%s)",
                tender_no, buyer_user_id, plan_label, demo,
            )
    except Exception as exc:
        logger.error(f"Buyer tender fit push failed buyer={buyer_user_id} tender={tender_no}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(name="buyer_tender_fit_push_sweep_task")
def buyer_tender_fit_push_sweep_task(demo: bool = False, lookback_minutes: int = 90):
    """Ingest-triggered sweep for per-tender high-fit pushes (#4).

    For every GeBIZ tender ingested in the last `lookback_minutes` (bounds the scan;
    the ledger, not the window, prevents duplicates), resolve its sector and match
    it against each buyer's watched-supplier sectors. On a match with no existing
    `BuyerTenderPush` row, enqueue a buyer-framed push and record the ledger. Only
    buyers on a Procurement plan are pushed to. Enqueued at the tail of
    `sync_gebiz_tenders` so pushes fire minutes after a tender first appears.
    """
    db = SessionLocal()
    enqueued = 0
    try:
        from datetime import datetime, timedelta
        from app.core.models import User
        from app.core.models import Organisation
        from app.core.models import GebizTender
        from app.core.models import BuyerTenderPush
        from app.services.buyer_procurement_insights import get_watchlist_sectors
        from app.services.tender_service import _CATEGORY_TO_SECTOR
        from app.billing.enforcement import PROCUREMENT_PLAN_KEYS

        cutoff = datetime.utcnow() - timedelta(minutes=lookback_minutes)
        new_tenders = (
            db.query(GebizTender)
            .filter(
                GebizTender.status == "Open",
                GebizTender.created_at.isnot(None),
                GebizTender.created_at >= cutoff,
            )
            .all()
        )
        if not new_tenders:
            return {"tenders": 0, "pushes": 0}

        # Precompute (tender, sector) once.
        scored: list = []
        for gt in new_tenders:
            raw = gt.raw_data or {}
            cat = raw.get("category", "") if isinstance(raw, dict) else ""
            sector = _CATEGORY_TO_SECTOR.get(cat, "General")
            scored.append((gt, sector))

        orgs = db.query(Organisation).all()
        for org in orgs:
            owner = db.query(User).filter(User.id == org.owner_user_id).first()
            if not owner or not owner.email:
                continue
            plan = (getattr(owner, "plan", "") or "").lower().strip()
            if plan not in PROCUREMENT_PLAN_KEYS:
                continue

            sectors = get_watchlist_sectors(db, str(owner.id))
            if not sectors:
                continue

            for gt, sector in scored:
                if sector not in sectors:
                    continue
                # Dedup: never push the same tender to the same buyer twice.
                exists = (
                    db.query(BuyerTenderPush)
                    .filter(
                        BuyerTenderPush.buyer_user_id == owner.id,
                        BuyerTenderPush.tender_no == gt.tender_no,
                    )
                    .first()
                )
                if exists:
                    continue
                db.add(BuyerTenderPush(
                    buyer_user_id=owner.id, tender_no=gt.tender_no,
                    sector=sector, pushed_at=datetime.utcnow(),
                ))
                db.commit()

                closing_label = gt.closing_date.strftime("%d %b %Y") if gt.closing_date else None
                buyer_tender_fit_push_task.delay(
                    str(owner.id), owner.email, gt.tender_no,
                    gt.title or "New government tender", gt.agency, gt.url,
                    closing_label, sector, sectors.get(sector, []),
                    product_type=plan, demo=demo,
                )
                enqueued += 1

        logger.info("[TenderPushSweep] %d new tenders, %d pushes enqueued (demo=%s)",
                    len(new_tenders), enqueued, demo)
        return {"tenders": len(new_tenders), "pushes": enqueued}
    except Exception as exc:
        logger.error(f"Buyer tender fit push sweep failed: {exc}")
        db.rollback()
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="buyer_demo_fireall_task")
def buyer_demo_fireall_task(
    buyer_user_id: str,
    buyer_email: str,
    product_type: str | None = None,
    override_company: str | None = None,
):
    """Demo/test-checkout fire-all — send EVERY buyer deliverable to one inbox.

    Triggered ONLY by a Stripe test-mode checkout (`livemode=false`); it must never
    fire for a real live buyer (the `_activate_subscription` gate enforces this).
    Its purpose is to let a client see, in one activation, every proactive email a
    buyer subscription can produce — so it fans out one representative copy of each
    deliverable, all `[DEMO]`-tagged and all running in `demo=True` mode:

      • #4 tender push  → a real just-ingested high-fit tender if any, else a sample.
      • #3 snapshot     → instant watchlist-add verification snapshot.
      • #2 certificate  → anchored Due-Diligence Certificate (mock tx hash, no gas).
      • #1 drift alert  → a sample supplier-status-change alert.

    With no real watchlist, each arm draws from the populated SG demo estate
    (`buyer_demo_samples`) so the deliverables render against varied, believable
    suppliers — a CRITICAL for the drift alert, a healthy one for the certificate,
    a FLAGGED one for the snapshot — rather than one shared placeholder.
      • Procurement Intelligence Digest (welcome framing).

    Deliverables that anchor to the chain use a deterministic mock hash in demo mode
    (`demo_tx_hash`), so the fire-all costs no gas while still showing the buyer the
    real artifact shape. Every arm is enqueued via `.delay()` so one failing arm
    can't block the others.
    """
    db = SessionLocal()
    fired = 0
    try:
        from app.core.models import Organisation, VendorWatchlistItem
        from app.core.models import GebizTender
        from app.services.tender_service import _CATEGORY_TO_SECTOR

        tier, plan_label = _buyer_tier_from_product(product_type)

        # ── Pick a representative supplier ──────────────────────────────────────
        # Prefer a real watched supplier so the demo mirrors the buyer's own estate;
        # fall back to a clearly-labelled sample so an empty watchlist still demos.
        sample_ref = None
        sample_name = None
        try:
            org = (
                db.query(Organisation)
                .filter(Organisation.owner_user_id == buyer_user_id)
                .first()
            )
            if org:
                it = (
                    db.query(VendorWatchlistItem)
                    .filter(VendorWatchlistItem.organisation_id == org.id)
                    .first()
                )
                if it:
                    sample_ref = it.vendor_ref
                    sample_name = it.vendor_name or it.vendor_ref
        except Exception:
            pass

        # No real watchlist → source each arm from the populated SG demo estate so
        # the drift / certificate / snapshot render against varied, believable
        # suppliers (a CRITICAL, a healthy, a FLAGGED) instead of one placeholder.
        # Each arm's row shape matches get_watched_suppliers_with_status.
        drift_ref = drift_name = None
        cert_ref = cert_name = None
        snap_ref = snap_name = None
        demo_matched: list[str] = []
        if not sample_ref:
            try:
                from app.services.buyer_demo_samples import (
                    demo_supplier,
                    demo_watched_suppliers,
                )
                crit = demo_supplier("critical")
                healthy = demo_supplier("healthy")
                flagged = demo_supplier("flagged")
                drift_ref, drift_name = crit["vendor_ref"], crit["vendor_name"]
                cert_ref, cert_name = healthy["vendor_ref"], healthy["vendor_name"]
                snap_ref, snap_name = flagged["vendor_ref"], flagged["vendor_name"]
                demo_matched = [r["vendor_name"] for r in demo_watched_suppliers(4)]
            except Exception as e:  # pragma: no cover
                logger.warning("[DemoFireAll] demo estate load failed: %s", e)
            # Legacy single-placeholder fallback if the demo estate can't load.
            sample_ref = "sample-supplier"
            sample_name = "Sample Supplier Pte Ltd"

        # Per-arm suppliers: real watchlist item if present, else the varied demo
        # estate, else the single placeholder.
        drift_ref = drift_ref or sample_ref
        drift_name = drift_name or sample_name
        cert_ref = cert_ref or sample_ref
        cert_name = cert_name or sample_name
        snap_ref = snap_ref or sample_ref
        snap_name = snap_name or sample_name
        matched_for_push = demo_matched or [sample_name]

        # ── Pick a representative tender ────────────────────────────────────────
        tender_no = tender_title = tender_agency = tender_url = closing_label = None
        sector = "General"
        try:
            t = (
                db.query(GebizTender)
                .filter(GebizTender.status == "Open")
                .order_by(GebizTender.created_at.desc())
                .first()
            )
            if t:
                tender_no = t.tender_no
                tender_title = t.title
                tender_agency = t.agency
                tender_url = t.url
                closing_label = t.closing_date.strftime("%d %b %Y") if t.closing_date else None
                cat = (t.raw_data or {}).get("category") if isinstance(t.raw_data, dict) else None
                sector = _CATEGORY_TO_SECTOR.get(cat, "General")
        except Exception:
            pass
        if not tender_no:
            tender_no = "SAMPLE-TENDER-0001"
            tender_title = "Supply and Delivery of Sample Goods and Services"
            tender_agency = "Sample Government Agency"
            tender_url = None
            closing_label = "30 days from now"
            sector = "General"

        # ── Fan out every deliverable in demo mode ──────────────────────────────
        try:
            buyer_tender_fit_push_task.delay(
                buyer_user_id, buyer_email, tender_no, tender_title,
                tender_agency, tender_url, closing_label, sector,
                matched_names=matched_for_push, product_type=product_type, demo=True,
            )
            fired += 1
        except Exception as e:  # pragma: no cover
            logger.warning("[DemoFireAll] tender push arm failed: %s", e)

        try:
            buyer_supplier_snapshot_task.delay(
                buyer_user_id, buyer_email, snap_ref, snap_name,
                None, product_type, is_certificate=False, demo=True,
            )
            fired += 1
        except Exception as e:  # pragma: no cover
            logger.warning("[DemoFireAll] snapshot arm failed: %s", e)

        try:
            buyer_supplier_snapshot_task.delay(
                buyer_user_id, buyer_email, cert_ref, cert_name,
                None, product_type, is_certificate=True, demo=True,
            )
            fired += 1
        except Exception as e:  # pragma: no cover
            logger.warning("[DemoFireAll] certificate arm failed: %s", e)

        try:
            buyer_supplier_drift_alert_task.delay(
                buyer_user_id, buyer_email, drift_ref, drift_name,
                "score_drop", f"{drift_name} — Trust score dropped",
                "<p>This supplier's Trust score fell in the latest scan. Review before "
                "your next award.</p>",
                product_type=product_type, demo=True,
            )
            fired += 1
        except Exception as e:  # pragma: no cover
            logger.warning("[DemoFireAll] drift alert arm failed: %s", e)

        try:
            buyer_procurement_digest_task.delay(
                buyer_user_id, buyer_email, product_type=product_type,
                override_company=override_company, is_first_cycle=True, demo=True,
            )
            fired += 1
        except Exception as e:  # pragma: no cover
            logger.warning("[DemoFireAll] digest arm failed: %s", e)

        logger.info(
            "[DemoFireAll] fired %d demo deliverables for buyer=%s tier=%s",
            fired, buyer_user_id, tier,
        )
        return {"fired": fired, "tier": tier}
    except Exception as exc:
        logger.error("Buyer demo fire-all failed for %s: %s", buyer_user_id, exc)
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="run_buyer_procurement_monthly_digests")
def run_buyer_procurement_monthly_digests():
    """
    Anniversary-day cron (runs daily; processes buyer subscribers whose
    anniversary day matches today's day-of-month) — mirrors
    `run_vendor_active_monthly_checks`. Sends each active buyer subscriber their
    single Procurement Intelligence Digest, tiered by their plan.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        from app.billing.enforcement import PROCUREMENT_PLAN_KEYS

        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_(list(PROCUREMENT_PLAN_KEYS)),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        # Latest product_type per user (for tier resolution); any buyer sub qualifies.
        product_by_user: dict = {}
        for s in active_subs:
            if s.user_id:
                product_by_user[s.user_id] = s.product_type
        user_ids = set(product_by_user.keys())
        subscribers = (
            db.query(User)
            .filter(
                User.id.in_(user_ids),
                _anniversary_match_filter(User.subscription_anniversary_day),
            )
            .all()
            if user_ids else []
        )
        for user in subscribers:
            if user.email:
                buyer_procurement_digest_task.delay(
                    str(user.id), user.email,
                    product_type=product_by_user.get(user.id),
                )
        logger.info(
            "[BuyerDigest] day=%d queued %d procurement digests",
            datetime.now(timezone.utc).day, len(subscribers),
        )
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="pdpa_monitor_monthly_alert_task")
def pdpa_monitor_monthly_alert_task(self, vendor_id: str, vendor_email: str):
    """
    Monthly PDPC regulatory alert for PDPA Monitor subscribers.
    Sends a plain-language summary of recent PDPC enforcement actions
    and guideline updates relevant to Singapore SMEs.
    Triggered on every invoice.payment_succeeded renewal cycle.
    """
    month_label = datetime.now(timezone.utc).strftime("%B %Y")
    from app.services.email_layout import branded_email_html, email_button
    body_html = branded_email_html(
        f"""
        <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">PDPA Monitor — {month_label} Regulatory Alert</h2>
        <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your monthly PDPA compliance briefing from BOOPPA.</p>
        <h3 style="color:#0f172a;font-size:16px;margin:0 0 8px;">Key PDPC Updates This Month</h3>
        <ul style="color:#334155;font-size:14px;line-height:1.6;padding-left:20px;margin:0 0 16px;">
          <li>Review your data breach notification procedures — PDPC enforcement actions increased 18% YoY.</li>
          <li>Ensure your Data Protection Officer (DPO) contact details are current on the PDPC register.</li>
          <li>Check that third-party data processors have signed updated data processing agreements.</li>
          <li>Verify your consent management records for any new marketing campaigns.</li>
        </ul>
        <h3 style="color:#0f172a;font-size:16px;margin:0 0 8px;">Action Items</h3>
        <ul style="color:#334155;font-size:14px;line-height:1.6;padding-left:20px;margin:0 0 20px;">
          <li>Log in to your BOOPPA dashboard to review your current compliance score.</li>
          <li>Upload any new compliance documents to maintain your verified status.</li>
        </ul>
        {email_button("https://www.booppa.io/vendor/dashboard", "View Dashboard →")}
        <p style="color:#64748b;font-size:12px;margin:8px 0 0;">
          This alert is part of your PDPA Monitor subscription.<br>
          booppa.io · Singapore
        </p>
        """,
        title=f"PDPA Monitor — {month_label}",
        preheader=f"Your {month_label} PDPA regulatory alert from BOOPPA.",
    )
    try:
        asyncio.run(EmailService().send_html_email(
            to_email=vendor_email,
            subject=f"BOOPPA PDPA Monitor — {month_label} Regulatory Alert",
            body_html=body_html,
        ))
    except Exception as exc:
        logger.error(f"[PDPAMonitorAlert] Email failed for {vendor_email}: {exc}")
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(bind=True, max_retries=2, name="pdpa_monitor_monthly_rescan_task")
def pdpa_monitor_monthly_rescan_task(self, vendor_id: str, vendor_email: str, website_url: str, override_company: str | None = None):
    """
    Monthly PDPA re-scan for PDPA Monitor subscribers.
    Creates a new PDPA report and queues fulfill_pdpa_task.
    Schedules a delayed drift check after the scan has had time to complete.

    `override_company` is test-harness-only (admin Test Identity); production
    monthly runs leave it None and use the stored profile company.
    """
    db = SessionLocal()
    try:
        from app.core.models import Report, User
        import uuid as _uuid

        user = UserRepository.get_by_id(db, str(vendor_id))
        company = (override_company or "").strip() or (getattr(user, "company", "Customer") if user else "Customer")

        # Atomic same-day reservation (closes the check-then-create race between
        # the daily anniversary cron and the Vendor-Pro quarterly cron, which can
        # both queue this task for the same vendor on a quarter-start day, and
        # between two concurrent test-checkout activations / a Stripe webhook
        # redelivery after idempotency rollback). Redis SET NX: exactly one caller
        # wins; the loser drops. This applies to ALL callers including test-harness
        # runs (override_company) — the same-day DB check below already blocks a
        # second scan on the same day, so exempting test runs bought no on-demand
        # rescan ability while leaving the race open. Degrades to the DB check
        # below when Redis is unavailable.
        from app.core.cache import cache as _cache
        _day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _lock_key = f"pdpa_rescan_lock:{vendor_id}:{_day}"
        if not _cache.add(_lock_key, {"queued": True}, ttl=86400):
            logger.info(f"[PdpaMonitor] Atomic drop: rescan already reserved today for {vendor_id}")
            return

        # Idempotency lock: drop if a scan is already pending or recently run (24h)
        from datetime import datetime, timezone, timedelta
        recent_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_scan = (
            db.query(Report)
            .filter(
                Report.owner_id == _uuid.UUID(vendor_id),
                Report.framework == "pdpa_quick_scan",
                Report.created_at >= recent_threshold
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        if recent_scan and isinstance(recent_scan.assessment_data, dict):
            if recent_scan.assessment_data.get("triggered_by") == "pdpa_monitor_monthly":
                logger.info(f"[PdpaMonitor] Idempotency drop: scan already exists for {vendor_id} within 24h")
                return

        stub = Report(
            owner_id=_uuid.UUID(vendor_id),
            framework="pdpa_quick_scan",
            company_name=company,
            company_website=website_url,
            status="pending",
            assessment_data={
                "payment_confirmed": True,
                "on_page_only": False,
                "tier": "PRO",
                "contact_email": vendor_email,
                "triggered_by": "pdpa_monitor_monthly",
            },
        )
        db.add(stub)
        db.commit()
        db.refresh(stub)

        from app.services.fulfillment import fulfill_pdpa
        # send_email=False: the month-over-month Monitor Report (queued below) is
        # the single consolidated PDPA deliverable email for this cycle — no
        # separate raw Quick-Scan email.
        asyncio.run(fulfill_pdpa(report_id=str(stub.id), customer_email=vendor_email, send_email=False))
        logger.info(f"PDPA Monitor monthly re-scan complete for vendor {vendor_id}")

        # Deliver the month-over-month Monitor Report PDF — the actual Monitor
        # deliverable (distinct from the one-off Quick Scan). Short countdown so
        # the just-completed report is fully committed first.
        run_pdpa_monitor_report_for_user.apply_async(
            args=[vendor_id, vendor_email], kwargs={"override_company": override_company},
            countdown=60,
        )

        # Drift detection: scan completion is async (process_report_task writes
        # results back to assessment_data). Defer drift check by 30 min so the
        # new report has a comparable risk_score before we diff it.
        check_compliance_drift_task.apply_async(
            args=[vendor_id, vendor_email, "pdpa_quick_scan"],
            countdown=1800,
        )

        # Vendor Pro: deliver the promised Quarterly PDPA Snapshot with drift as a
        # PDF attachment. This task is the single point Vendor Pro scans flow
        # through (activation + the Jan/Apr/Jul/Oct quarterly cron), so gating on
        # plan here yields the promised quarterly cadence. Deferred like the drift
        # check so the just-queued scan has completed before we diff it.
        if user and (getattr(user, "plan", "") or "") == "vendor_pro":
            run_vendor_pro_pdpa_snapshot_for_user.apply_async(
                args=[vendor_id, vendor_email],
                kwargs={"override_company": override_company},
                countdown=1860,
            )
    except Exception as exc:
        logger.error(f"PDPA Monitor monthly re-scan failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=600)
    finally:
        db.close()


# Generic fallback briefing — used when an org has no open findings, or when the
# LLM is unavailable, so Monitor delivery never blocks on the AI call.
_GENERIC_MONITOR_BRIEFING = [
    "Confirm your data-breach notification path meets PDPA §26D (notify PDPC within 3 calendar days).",
    "Verify your DPO contact details are current and published.",
    "Check that third-party processors have signed up-to-date data-processing agreements.",
]


def _pdpa_monitor_briefing_bullets(company, sector, findings, current_score, previous_score):
    """Three personalised regulatory action items for the Monitor briefing.

    Keyed on the org's actual open findings + sector via DeepSeek (BooppaAIService).
    Falls back to the generic checklist when there are no findings or the model is
    unavailable. Returned strings are XML-escaped and safe to drop into <li> tags.
    """
    def _xe(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    import re as _re

    # Extract the normalised PDPA section tokens ("24", "26D", "11") from a
    # legislation string, ignoring sub-clauses. This is the authoritative set the
    # email briefing is allowed to cite — anything else the model emits is a
    # hallucination and must be dropped (closes the PDF-vs-email citation
    # mismatch: the PDF renders the finding's own legislation, so the email must
    # cite from the same source, not invent section numbers).
    def _sections(text):
        return {m.upper() for m in _re.findall(r"(?:§|s\.?)\s*(\d+[A-Z]?)", str(text or ""))}

    flist = findings if isinstance(findings, list) else []
    if not flist:
        return _GENERIC_MONITOR_BRIEFING

    lines = []
    allowed_sections: set[str] = set()
    deterministic: list[str] = []
    for f in flist[:8]:
        if not isinstance(f, dict):
            continue
        sev = (f.get("severity") or "").upper()
        dim = f.get("dimension") or f.get("type") or f.get("category") or "finding"
        desc = f.get("title") or f.get("description") or ""
        leg = f.get("legislation") or f.get("legislation_violated") or ""
        leg_str = f" (Legislation: {leg})" if leg else ""
        lines.append(f"- [{sev}] {dim}: {desc}{leg_str}".strip())
        allowed_sections |= _sections(leg)
        # Deterministic backfill bullet — cites the finding's own legislation, so
        # it can never diverge from the PDF.
        if leg and leg.strip().upper() not in ("", "N/A"):
            deterministic.append(_xe(f"Remediate {dim}: {desc or 'address the flagged gap'} ({leg})."))
    findings_blob = "\n".join(lines) or "(no structured findings)"

    def _validate(bullets: list) -> list:
        """Keep only bullets whose PDPA section citations are all in the
        authoritative allow-list; a bullet with no section citation is advisory
        (nothing to mismatch) and is kept."""
        out = []
        for b in bullets:
            cited = _sections(b)
            if cited and not cited.issubset(allowed_sections):
                continue  # cites a section not grounded in the findings — drop
            out.append(b)
        return out

    if isinstance(current_score, int) and isinstance(previous_score, int):
        delta = f" Compliance score moved {previous_score} -> {current_score}/100."
    elif isinstance(current_score, int):
        delta = f" Current compliance score {current_score}/100."
    else:
        delta = ""

    allow_str = ", ".join(f"§{s}" for s in sorted(allowed_sections)) or "(none provided)"
    try:
        import json as _json

        from app.services.booppa_ai_service import BooppaAIService
        ai = BooppaAIService()
        system = (
            "You are a Singapore PDPA compliance advisor. Given an organisation's open "
            "findings, produce EXACTLY 3 specific, actionable regulatory items. Each item: "
            "the concrete action, the relevant PDPA section reference, and an estimated "
            "remediation time. Be specific to the findings — no generic advice. "
            "CRITICAL: You MUST use the exact Legislation provided in the findings. "
            "Do NOT invent or hallucinate section numbers. "
            f"The ONLY PDPA sections you may cite are: {allow_str}. Do not cite any other section. "
            "Return ONLY a JSON array of 3 short strings, no prose, no code fences."
        )
        user = (
            f"Organisation: {company}\nSector: {sector or 'unknown'}\n"
            f"Open findings:\n{findings_blob}\n{delta}\n"
            "Return 3 action items as a JSON array of strings."
        )
        raw = asyncio.run(ai._call_deepseek([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]))
        if raw:
            txt = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
            cand = _json.loads(txt)
            if isinstance(cand, dict):
                cand = next((v for v in cand.values() if isinstance(v, list)), None)
            if isinstance(cand, list):
                bullets = [_xe(str(x)) for x in cand if str(x).strip()][:3]
                # Drop any bullet citing a section not grounded in the findings,
                # then backfill from the finding-derived deterministic bullets so
                # the email never contradicts the PDF's citations.
                bullets = _validate(bullets)
                for d in deterministic:
                    if len(bullets) >= 3:
                        break
                    if d not in bullets:
                        bullets.append(d)
                if bullets:
                    return bullets[:3]
    except Exception as exc:
        logger.warning("[MonitorReport] personalised briefing failed: %s — using generic", exc)
    # Findings exist but the model failed: prefer deterministic finding-grounded
    # bullets over the fixed generic list (which hard-codes §26D regardless of the
    # org's actual findings, the second citation-drift vector).
    if deterministic:
        return deterministic[:3]
    return _GENERIC_MONITOR_BRIEFING


@celery_app.task(bind=True, max_retries=2, name="run_pdpa_monitor_report_for_user")
def run_pdpa_monitor_report_for_user(self, vendor_id: str, vendor_email: str | None = None, override_company: str | None = None):
    """Generate + deliver the month-over-month PDPA Monitor Report PDF.

    This is the actual Monitor deliverable: a comparison of the two most recent
    scans (current compliance score, change vs last month, dimension moves) —
    distinct from the one-off Quick Scan the activation email used to link. On
    the first cycle (only one scan) it ships a baseline edition.

    `override_company` is test-harness-only (admin Test Identity); production
    leaves it None and uses the stored profile company.
    """
    from app.core.models import Report, User
    from app.services.compliance_drift import _extract_risk_score, _per_dimension_flips
    from app.services.pdpa_monitor_delta_generator import generate_pdpa_monitor_report_pdf
    from app.services.storage import S3Service

    db = SessionLocal()
    try:
        user = UserRepository.get_by_id(db, str(vendor_id))
        if not user:
            logger.warning("[MonitorReport] no user for id=%s", vendor_id)
            return
        email = vendor_email or user.email
        if not email:
            return
        company = (override_company or "").strip() or (getattr(user, "company", "") or "").strip() or "Your Organisation"
        framework = "pdpa_quick_scan"

        reports = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.completed_at.desc().nullslast())
            .limit(2)
            .all()
        )
        if not reports:
            logger.info("[MonitorReport] no completed PDPA report yet for %s — skipping", email)
            return
        current = reports[0]
        previous = reports[1] if len(reports) > 1 else None

        def _compliance(r) -> int | None:
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            # Prefer the canonical persisted score (single source of truth, C2);
            # fall back to 100 - risk_score.
            cs = ad.get("compliance_score")
            if isinstance(cs, (int, float)):
                return int(round(cs))
            risk = _extract_risk_score(r)
            return None if risk is None else max(0, min(100, 100 - int(round(risk))))

        cur_ad = current.assessment_data if isinstance(current.assessment_data, dict) else {}
        # Single source of truth — the Quick Scan nests findings under
        # assessment_data["booppa_report"]["detailed_findings"]. Reading only the
        # top-level keys here is what produced the "0 vs 2 open findings"
        # contradiction between the Monitor Report and the Quick Scan.
        findings = resolve_pdpa_findings(cur_ad)
        dimension_changes = (
            _per_dimension_flips(db, str(user.id), framework, current.id, previous.id)
            if previous else []
        )
        scanned_url = cur_ad.get("display_url") or cur_ad.get("website_url") or current.company_website

        # Personalised regulatory briefing — keyed on THIS org's open findings +
        # sector (replaces the generic 3-bullet checklist that was identical for
        # every client; forensic-audit finding).
        from app.core.models import VendorSector
        _sec_row = db.query(VendorSector).filter(VendorSector.vendor_id == user.id).first()
        _sector = _sec_row.sector if _sec_row else None
        briefing_bullets = _pdpa_monitor_briefing_bullets(
            company, _sector, findings, _compliance(current),
            _compliance(previous) if previous else None,
        )
        briefing_html = "".join(f"<li>{b}</li>" for b in briefing_bullets)

        # ── Compliance trend (6b) + finding age for urgency counter (6c) ──────
        history_reports = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.completed_at.asc().nullsfirst())
            .limit(12)
            .all()
        )

        def _fkey(f: dict) -> str:
            return str((f.get("type") or f.get("dimension") or f.get("title") or "")).strip().lower()

        score_history_dict = {}
        first_seen: dict[str, datetime] = {}
        for r in history_reports:  # ascending → earliest occurrence wins
            when = r.completed_at or r.created_at
            sc = _compliance(r)
            if sc is not None and when is not None:
                m_key = when.strftime("%Y-%m")
                score_history_dict[m_key] = {"label": when.strftime("%b '%y"), "score": sc}
            if when is None:
                continue
            rfs = resolve_pdpa_findings(r.assessment_data)
            for f in (rfs if isinstance(rfs, list) else []):
                if isinstance(f, dict):
                    k = _fkey(f)
                    if k and k not in first_seen:
                        first_seen[k] = when

        score_history = list(score_history_dict.values())

        urgent_findings = []
        _now_naive = datetime.utcnow()
        for f in (findings if isinstance(findings, list) else []):
            if not isinstance(f, dict) or (f.get("severity") or "").upper() != "HIGH":
                continue
            seen = first_seen.get(_fkey(f))
            if not seen:
                continue
            seen_n = seen.replace(tzinfo=None) if getattr(seen, "tzinfo", None) else seen
            days_open = (_now_naive - seen_n).days
            if days_open > 14:
                urgent_findings.append({
                    "label": (f.get("title") or f.get("type") or _fkey(f)).replace("_", " ").title(),
                    "days_open": days_open,
                    "severity": "HIGH",
                })
        urgent_findings.sort(key=lambda x: x["days_open"], reverse=True)

        pdf_bytes = generate_pdpa_monitor_report_pdf({
            "company_name": company,
            "current_score": _compliance(current),
            "previous_score": _compliance(previous) if previous else None,
            "scanned_url": scanned_url,
            "findings_count": len(findings) if isinstance(findings, list) else None,
            "dimension_changes": dimension_changes,
            "full_report_url": f"https://api.booppa.io/api/v1/reports/{current.id}/download",
            "urgent_findings": urgent_findings,
            "score_history": score_history,
        })

        report_url = None
        try:
            report_url = asyncio.run(
                S3Service().upload_pdf(pdf_bytes, f"pdpa-monitor-report-{current.id}")
            )
        except Exception as up_err:
            logger.error("[MonitorReport] S3 upload failed for %s: %s", email, up_err)

        if pdf_bytes:
            month_label = datetime.now(timezone.utc).strftime("%B %Y")
            # The PDF is attached directly to the email (audit fix: the report must
            # arrive as a downloadable file, not a dashboard-only link). The S3 link,
            # when available, is kept as a secondary access path.
            from app.services.email_layout import branded_email_html, email_button
            link_block = (
                email_button(report_url, "View your Monitor Report online")
                if report_url else ""
            )
            body_html = branded_email_html(
                f"""
                <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">PDPA Monitor Report — {month_label}</h2>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company}</strong>,</p>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your monthly PDPA Monitor report is <strong>attached to this email as a PDF</strong> —
                   it compares this month's scan against last month's, so you can see exactly what moved.</p>
                {link_block}
                <h3 style="color:#0f172a;font-size:15px;margin:24px 0 6px;">This month's regulatory briefing</h3>
                <ul style="font-size:13px;color:#334155;line-height:1.6;margin:0 0 8px;padding-left:18px;">
                  {briefing_html}
                </ul>
                <p style="color:#64748b;font-size:12px;margin:16px 0 0;">PDPA Monitor — monthly compliance tracking + regulatory briefing · booppa.io</p>
                """,
                title=f"PDPA Monitor Report — {month_label}",
                preheader=f"Your {month_label} PDPA Monitor report is attached.",
            )
            _safe_company = (company or "report").replace("/", "-").replace(" ", "-")
            sent = asyncio.run(EmailService().send_html_email(
                to_email=email,
                subject=f"Your PDPA Monitor Report — {month_label}",
                body_html=body_html,
                attachments=[(f"PDPA-Monitor-Report-{_safe_company}-{month_label}.pdf", pdf_bytes)],
            ))
            if not sent:
                logger.error("[MonitorReport] delivery email rejected for %s", email)
            else:
                logger.info("[MonitorReport] Delivered to %s (previous=%s)", email, bool(previous))
    except Exception as exc:
        logger.error("[MonitorReport] Failed for %s: %s", vendor_id, exc)
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="run_vendor_pro_pdpa_snapshot_for_user")
def run_vendor_pro_pdpa_snapshot_for_user(self, vendor_id: str, vendor_email: str | None = None, override_company: str | None = None):
    """Generate + deliver Vendor Pro's Quarterly PDPA Snapshot with drift (PDF).

    Closes the audit gap "PDPA Quarterly Snapshot with drift for Vendor Pro — not
    attached". Compares the vendor's two most recent completed PDPA scans, renders
    a one-page drift PDF, anchors it on the existing (Amoy testnet) chain, and
    emails it as a direct attachment. Baseline edition when only one scan exists.

    `override_company` is test-harness-only (admin Test Identity).
    """
    from app.core.models import Report, User
    from app.services.compliance_drift import _extract_risk_score, _per_dimension_flips
    from app.services.vendor_pdpa_snapshot_generator import generate_vendor_pdpa_snapshot_pdf
    from app.services.storage import S3Service

    db = SessionLocal()
    try:
        user = UserRepository.get_by_id(db, str(vendor_id))
        if not user:
            logger.warning("[VendorProSnapshot] no user for id=%s", vendor_id)
            return
        email = vendor_email or user.email
        if not email:
            return
        company = (override_company or "").strip() or (getattr(user, "company", "") or "").strip() or "Your Organisation"
        framework = "pdpa_quick_scan"

        reports = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.completed_at.desc().nullslast())
            .limit(2)
            .all()
        )
        if not reports:
            logger.info("[VendorProSnapshot] no completed PDPA report yet for %s — skipping", email)
            return
        current = reports[0]
        previous = reports[1] if len(reports) > 1 else None

        def _compliance(r) -> int | None:
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            cs = ad.get("compliance_score")
            if isinstance(cs, (int, float)):
                return int(round(cs))
            risk = _extract_risk_score(r)
            return None if risk is None else max(0, min(100, 100 - int(round(risk))))

        cur_ad = current.assessment_data if isinstance(current.assessment_data, dict) else {}
        # Single source of truth — the Quick Scan nests findings under
        # assessment_data["booppa_report"]["detailed_findings"]. Reading only the
        # top-level keys here is what produced the "0 vs 2 open findings"
        # contradiction between the Monitor Report and the Quick Scan.
        findings = resolve_pdpa_findings(cur_ad)
        dimension_flips = (
            _per_dimension_flips(db, str(user.id), framework, current.id, previous.id)
            if previous else []
        )
        scanned_url = cur_ad.get("display_url") or cur_ad.get("website_url") or current.company_website

        pdf_bytes = generate_vendor_pdpa_snapshot_pdf({
            "company_name": company,
            "scanned_url": scanned_url,
            "current_score": _compliance(current),
            "previous_score": _compliance(previous) if previous else None,
            "current_risk": _extract_risk_score(current),
            "previous_risk": _extract_risk_score(previous) if previous else None,
            "dimension_flips": dimension_flips,
            "findings_count": len(findings) if isinstance(findings, list) else None,
            "is_baseline": previous is None,
        })

        # Anchor the snapshot hash on the existing chain (Amoy testnet under Lean
        # Mode) — best-effort; the PDF discloses the network honestly.
        anchor_tx = None
        try:
            import hashlib
            from app.services.blockchain import BlockchainService
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            anchor_tx = asyncio.run(BlockchainService().anchor_evidence(
                digest, metadata=f"vendor_pro_pdpa_snapshot:{current.id}"))
        except Exception as anchor_err:
            logger.warning("[VendorProSnapshot] anchoring failed for %s: %s", email, anchor_err)

        if anchor_tx:
            # Re-render with the anchor tx disclosed on the PDF.
            pdf_bytes = generate_vendor_pdpa_snapshot_pdf({
                "company_name": company,
                "scanned_url": scanned_url,
                "current_score": _compliance(current),
                "previous_score": _compliance(previous) if previous else None,
                "current_risk": _extract_risk_score(current),
                "previous_risk": _extract_risk_score(previous) if previous else None,
                "dimension_flips": dimension_flips,
                "findings_count": len(findings) if isinstance(findings, list) else None,
                "is_baseline": previous is None,
                "anchor_tx": anchor_tx,
            })

        try:
            asyncio.run(S3Service().upload_pdf(pdf_bytes, f"vendor-pro-pdpa-snapshot-{current.id}"))
        except Exception as up_err:
            logger.error("[VendorProSnapshot] S3 upload failed for %s: %s", email, up_err)

        edition = "Baseline" if previous is None else "Drift"
        quarter_label = datetime.now(timezone.utc).strftime("%b %Y")
        from app.services.email_layout import branded_email_html
        body_html = branded_email_html(
            f"""
            <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Quarterly PDPA Snapshot</h2>
            <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company}</strong>,</p>
            <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your Vendor Pro <strong>Quarterly PDPA Snapshot with drift</strong> is
               <strong>attached to this email as a PDF</strong>. It compares your latest PDPA scan
               against the previous one and flags any dimensions that moved.</p>
            <p style="color:#64748b;font-size:12px;margin:0;">Vendor Pro — quarterly PDPA drift tracking · booppa.io</p>
            """,
            title="Your Quarterly PDPA Snapshot",
            preheader="Your quarterly PDPA drift snapshot is attached.",
        )
        _safe_company = (company or "vendor").replace("/", "-").replace(" ", "-")
        sent = asyncio.run(EmailService().send_html_email(
            to_email=email,
            subject=f"Your Quarterly PDPA Snapshot — {quarter_label}",
            body_html=body_html,
            attachments=[(f"PDPA-Snapshot-{_safe_company}-{quarter_label}.pdf", pdf_bytes)],
        ))
        if not sent:
            logger.error("[VendorProSnapshot] delivery email rejected for %s", email)
        else:
            logger.info("[VendorProSnapshot] Delivered %s edition to %s (flips=%d)",
                        edition, email, len(dimension_flips))
    except Exception as exc:
        logger.error("[VendorProSnapshot] Failed for %s: %s", vendor_id, exc)
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=1, name="check_compliance_drift_task")
def check_compliance_drift_task(self, vendor_id: str, vendor_email: str, framework: str = "pdpa_quick_scan"):
    """
    Compare the latest two completed PDPA reports for this vendor.
    On material drift, persist a ComplianceDriftEvent and email the vendor.
    """
    from app.services.compliance_drift import detect_drift_for_vendor
    from app.core.models import ComplianceDriftEvent

    db = SessionLocal()
    email_svc = EmailService()
    try:
        result = detect_drift_for_vendor(db, vendor_id, framework=framework)
        if not result:
            return

        if not vendor_email:
            return

        prev = result["previous_score"]
        cur = result["current_score"]
        delta_pct = result["delta_pct"]
        severity = result["severity"]
        dim_flips = result.get("dimension_flips") or []
        color = "#dc2626" if severity == "CRITICAL" else "#d97706"

        # Tier 6: pull confirmed remediations since the previous report so we
        # can credit the user's work in the same email.
        confirmed_rems: list = []
        try:
            from app.core.models import FindingRemediation
            from app.services.finding_keys import label_for_key as _label_for_key
            from app.core.models import Report as _Report
            prev_report = (
                db.query(_Report)
                .filter(
                    _Report.owner_id == vendor_id,
                    _Report.framework == framework,
                    _Report.status == "completed",
                )
                .order_by(_Report.created_at.desc())
                .offset(1).limit(1).first()
            )
            since = prev_report.completed_at or prev_report.created_at if prev_report else None
            q = db.query(FindingRemediation).filter(
                FindingRemediation.vendor_id == vendor_id,
                FindingRemediation.confirmation_status == "confirmed",
            )
            if since:
                q = q.filter(FindingRemediation.confirmed_at >= since)
            confirmed_rems = [
                {"label": _label_for_key(r.finding_key), "key": r.finding_key}
                for r in q.order_by(FindingRemediation.confirmed_at.desc()).limit(8).all()
            ]
        except Exception as rem_exc:
            logger.warning("[ComplianceDrift] Could not load confirmed remediations: %s", rem_exc)

        # Tier 6: improvements block — credit confirmed remediations
        if confirmed_rems:
            improvements_rows = "".join(
                f'<tr><td style="padding:4px 0;color:#065f46;">✓ {r["label"]}</td></tr>'
                for r in confirmed_rems
            )
            improvements_block = f"""
          <h2 style="margin:24px 0 8px;font-size:14px;color:#065f46;">Improvements since your last scan</h2>
          <table style="width:100%;border-collapse:collapse;margin:8px 0 16px;font-size:13px;">
            {improvements_rows}
          </table>"""
        else:
            improvements_block = ""

        # Tier 4: per-dimension flip block. Empty string when no flips so
        # the email layout collapses cleanly for score-only drift events.
        if dim_flips:
            flip_rows = "".join(
                f'<tr><td style="padding:6px 0;color:#0f172a;">{f["dimension_name"]}</td>'
                f'<td style="padding:6px 0;text-align:right;color:#64748b;">{f["previous_status"]}</td>'
                f'<td style="padding:6px 0;text-align:right;font-weight:bold;color:{color};">→ {f["current_status"]}</td>'
                f'</tr>'
                for f in dim_flips
            )
            dim_block = f"""
          <h2 style="margin:24px 0 8px;font-size:14px;color:#0f172a;">Dimensions that regressed</h2>
          <table style="width:100%;border-collapse:collapse;margin:8px 0 16px;font-size:13px;">
            <tr><th style="text-align:left;padding:6px 0;color:#64748b;font-weight:normal;">Dimension</th>
                <th style="text-align:right;padding:6px 0;color:#64748b;font-weight:normal;">Was</th>
                <th style="text-align:right;padding:6px 0;color:#64748b;font-weight:normal;">Now</th></tr>
            {flip_rows}
          </table>"""
        else:
            dim_block = ""

        from app.services.email_layout import branded_email_html, email_button
        body_html = branded_email_html(
            f"""
          <h2 style="margin:0 0 12px;font-size:20px;color:{color};">PDPA compliance drift detected — {severity}</h2>
          <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your latest monthly PDPA Monitor scan shows a material change in risk posture.</p>
          <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <tr><td style="padding:8px 0;color:#64748b;">Previous risk score</td>
                <td style="padding:8px 0;text-align:right;font-weight:bold;">{prev:.1f}</td></tr>
            <tr><td style="padding:8px 0;color:#64748b;">Current risk score</td>
                <td style="padding:8px 0;text-align:right;font-weight:bold;color:{color};">{cur:.1f}</td></tr>
            <tr><td style="padding:8px 0;color:#64748b;">Change</td>
                <td style="padding:8px 0;text-align:right;font-weight:bold;color:{color};">+{delta_pct:.1f}%</td></tr>
          </table>{improvements_block}{dim_block}
          <p style="font-size:14px;color:#475569;line-height:1.6;margin:0 0 20px;">
            Higher risk scores indicate degraded PDPA posture. Review the latest report
            and address the highlighted findings to restore your compliance baseline.
          </p>
          {email_button("https://www.booppa.io/vendor/dashboard", "Review report", primary=False)}
          <p style="margin:8px 0 0;font-size:11px;color:#94a3b8;">
            You're receiving this because you subscribe to PDPA Monitor.
          </p>
            """,
            title=f"PDPA compliance drift — {severity}",
            preheader="Your latest PDPA Monitor scan shows a change in risk posture.",
        )

        try:
            asyncio.run(email_svc.send_html_email(
                to_email=vendor_email,
                subject=f"PDPA compliance drift — {severity}",
                body_html=body_html,
            ))
            event = db.query(ComplianceDriftEvent).filter(
                ComplianceDriftEvent.id == result["event_id"]
            ).first()
            if event:
                event.notified = True
                db.commit()
        except Exception as email_exc:
            logger.warning(f"[ComplianceDrift] Email failed for {vendor_email}: {email_exc}")
    except Exception as exc:
        logger.error(f"[ComplianceDrift] Drift check failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=600)
    finally:
        db.close()


def _anniversary_match_filter(model_attr, now=None):
    """Build an SQLAlchemy filter expression that matches subscribers whose
    anniversary day fires today. Handles short-month edges:
      • Regular day → anniversary == today.day
      • Last day of a 28/29/30-day month → anniversary >= today.day
        (so Jan-31 subscriber fires on Feb 28, Apr 30, etc.)

    `model_attr` is the SQLAlchemy column reference (e.g.
    `User.subscription_anniversary_day`).
    """
    import calendar as _cal
    from sqlalchemy import or_ as _or
    now = now or datetime.now(timezone.utc)
    last_day = _cal.monthrange(now.year, now.month)[1]
    if now.day < last_day:
        return model_attr == now.day
    # Last day of this month — sweep up anyone whose nominal anniversary
    # falls on a day this month doesn't have.
    return model_attr >= now.day


@celery_app.task(name="check_vendor_proof_expiry")
def check_vendor_proof_expiry():
    """Daily sweep for Vendor Proof certificate expiry.

    (1) Marks lapsed VerifyRecords EXPIRED so the public verify page reports
        "Expired" instead of a stale "active" badge.
    (2) Emails a renewal reminder 30 calendar days before expiry (one-day match
        window → each record reminded once).

    Expiry dates are stored as naive UTC (the models_v6 convention), so all
    comparisons here use naive `datetime.utcnow()`.
    """
    from app.core.models import User
    from app.core.models import VerifyRecord, LifecycleStatus

    db = SessionLocal()
    reminded = 0
    expired = 0
    try:
        now = datetime.utcnow()

        # (1) Mark lapsed certificates expired.
        lapsed = (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.expires_at != None,  # noqa: E711
                VerifyRecord.expires_at < now,
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .all()
        )
        for v in lapsed:
            v.lifecycle_status = LifecycleStatus.EXPIRED
            expired += 1
        if lapsed:
            db.commit()

        # (2) Renewal reminders for certificates expiring in exactly 30 days.
        target = (now + timedelta(days=30)).date()
        active = (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.expires_at != None,  # noqa: E711
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .all()
        )
        email_svc = EmailService()
        for v in active:
            if not v.expires_at or v.expires_at.date() != target:
                continue
            user = db.query(User).filter(User.id == v.vendor_id).first()
            if not user or not user.email:
                continue
            company = v.company_name or getattr(user, "company", None) or "your company"
            exp_str = v.expires_at.strftime("%d %B %Y")
            from app.services.email_layout import branded_email_html, email_button
            body_html = branded_email_html(
                f"""
                <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Vendor Proof expires in 30 days</h2>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company}</strong>,</p>
                <p style="margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;">Your Booppa Vendor Proof certificate is valid until <strong>{exp_str}</strong>.
                   Renew now to keep your verification active — a renewal reruns your PDPA
                   scan so the certificate reflects your current compliance standing.</p>
                {email_button("https://www.booppa.io/vendor/dashboard", "Renew Vendor Proof →")}
                <p style="color:#64748b;font-size:12px;margin:8px 0 0;">
                  After {exp_str}, your public verification page will show "Expired" until you renew. · booppa.io
                </p>
                """,
                title="Your Vendor Proof expires in 30 days",
                preheader=f"Renew your Vendor Proof before {exp_str} to stay verified.",
            )
            try:
                ok = asyncio.run(email_svc.send_html_email(
                    to_email=user.email,
                    subject=f"Your Vendor Proof expires {exp_str} — renew to stay verified",
                    body_html=body_html,
                ))
                if ok:
                    reminded += 1
                else:
                    logger.error("[VendorProofExpiry] reminder rejected for %s", user.email)
            except Exception as _e:
                logger.warning("[VendorProofExpiry] reminder failed for %s: %s", user.email, _e)
    except Exception as exc:
        logger.error("[VendorProofExpiry] sweep failed: %s", exc)
    finally:
        db.close()
    logger.info("[VendorProofExpiry] reminded=%d expired=%d", reminded, expired)


@celery_app.task(name="run_vendor_active_monthly_checks")
def run_vendor_active_monthly_checks():
    """
    Anniversary-day cron (runs daily; processes subscribers whose anniversary
    day matches today's day-of-month). Replaces the calendar-1st delivery so
    every subscriber gets their health check on the same day each month they
    actually subscribed.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_([
                "vendor_active_monthly", "vendor_active_annual", "vendor_active",
                # Vendor Pro inherits Vendor Active health checks.
                "vendor_pro_monthly", "vendor_pro_annual", "vendor_pro",
            ]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = (
            db.query(User)
            .filter(
                User.id.in_(user_ids),
                _anniversary_match_filter(User.subscription_anniversary_day),
            )
            .all()
            if user_ids else []
        )
        for user in subscribers:
            if user.email:
                vendor_active_health_check_task.delay(str(user.id), user.email)
        logger.info(
            "[VendorActive] day=%d queued %d health checks",
            datetime.now(timezone.utc).day, len(subscribers),
        )
    finally:
        db.close()


@celery_app.task(name="run_pdpa_monitor_monthly_rescans")
def run_pdpa_monitor_monthly_rescans():
    """
    Anniversary-day cron — same pattern as run_vendor_active_monthly_checks.
    Runs daily; only processes PDPA Monitor subscribers whose anniversary day
    matches today's day-of-month.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_(["pdpa_monitor_monthly", "pdpa_monitor_annual", "pdpa_monitor"]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = (
            db.query(User)
            .filter(
                User.id.in_(user_ids),
                _anniversary_match_filter(User.subscription_anniversary_day),
            )
            .all()
            if user_ids else []
        )
        queued = 0
        for user in subscribers:
            website = getattr(user, "website", "") or ""
            if user.email and website:
                pdpa_monitor_monthly_rescan_task.delay(str(user.id), user.email, website)
                queued += 1
        logger.info(
            "[PdpaMonitor] day=%d queued %d/%d rescans",
            datetime.now(timezone.utc).day, queued, len(subscribers),
        )
    finally:
        db.close()


@celery_app.task(name="run_vendor_pro_quarterly_pdpa_rescans")
def run_vendor_pro_quarterly_pdpa_rescans():
    """
    Beat task: runs on the 1st of Jan/Apr/Jul/Oct.
    Finds all Vendor Pro subscribers and queues a PDPA re-scan for each.

    Uses the same pdpa_monitor_monthly_rescan_task because the per-user scan
    logic is identical — only the cadence differs (quarterly instead of
    monthly). Subscribers without a `website` set are skipped.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_([
                "vendor_pro_monthly", "vendor_pro_annual", "vendor_pro",
            ]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
        queued = 0
        for user in subscribers:
            website = getattr(user, "website", "") or ""
            if user.email and website:
                pdpa_monitor_monthly_rescan_task.delay(str(user.id), user.email, website)
                queued += 1
        logger.info(f"[VendorProPDPA] Queued quarterly PDPA rescans for {queued}/{len(subscribers)} Vendor Pro subscribers")
    finally:
        db.close()


@celery_app.task(name="run_compliance_evidence_monthly_refresh")
def run_compliance_evidence_monthly_refresh():
    """
    Anniversary-day cron — runs daily, processes Compliance Evidence subscribers
    whose anniversary matches today's day-of-month.

    Reset semantics each cycle:
      - `signed_cover_sheet_uploaded` flipped back to False (last month's signed
        sheet stays on record but is out-of-cycle for the new cover sheet).
      - `compliance_evidence_credits` is normalised to 1 (does NOT roll over —
        subscription gives one signed-CS upload per month).
      - `pending_cover_sheet` re-flipped True so the new cycle re-fires the
        cover-sheet readiness gate after fresh PDPA + RFP regenerate.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        from app.workers.tasks import fulfill_bundle_task

        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_(["compliance_evidence_monthly", "compliance_evidence"]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = (
            db.query(User)
            .filter(
                User.id.in_(user_ids),
                _anniversary_match_filter(User.subscription_anniversary_day),
            )
            .all()
            if user_ids else []
        )

        eligible: list = []
        missing_website: list = []
        for user in subscribers:
            if not user.email:
                continue
            website = (getattr(user, "website", "") or "").strip()
            if not website:
                missing_website.append(user)
                continue
            user.signed_cover_sheet_uploaded = False
            user.compliance_evidence_credits = 1
            user.pending_cover_sheet = True
            eligible.append((user, website))
        db.commit()

        for user, website in eligible:
            fulfill_bundle_task.delay(
                product_type="compliance_evidence_pack",
                session_id=None,
                customer_email=user.email,
                metadata={
                    "company_name": getattr(user, "company", ""),
                    "vendor_url": website,
                    "subscription_cycle": True,
                },
                report_id=None,
            )

        # Prompt subscribers missing a website to backfill their profile —
        # without `vendor_url` the PDPA scan + RFP regen has no target, so we
        # skip the cycle for them rather than fail downstream.
        if missing_website:
            email_svc = EmailService()
            for user in missing_website:
                logger.warning(
                    f"[CE Refresh] Skipping {user.email} — no website on profile; cycle deferred"
                )
                from app.services.email_layout import branded_email_html, email_button
                body_html = branded_email_html(
                    """
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Action required: add your website</h2>
                  <p style="margin:0 0 20px;color:#334155;font-size:15px;line-height:1.6;">
                    Your Compliance Evidence subscription cycle could not run this month because
                    no website is on your profile. We need it to refresh your PDPA Snapshot and
                    RFP Complete Kit before issuing this month's Cover Sheet.
                  </p>
                  """
                    + email_button("https://www.booppa.io/vendor/profile", "Update profile", primary=False)
                    + """
                  <p style="margin:8px 0 0;font-size:11px;color:#94a3b8;">
                    Once your website is saved, your next cycle will resume automatically.
                  </p>
                    """,
                    title="Action required: add your website",
                    preheader="Add your website to resume your monthly Compliance Evidence cycle.",
                )
                try:
                    asyncio.run(email_svc.send_html_email(
                        to_email=user.email,
                        subject="Compliance Evidence: add your website to resume monthly cycle",
                        body_html=body_html,
                    ))
                except Exception as email_exc:
                    logger.warning(f"[CE Refresh] Could not email {user.email}: {email_exc}")

        logger.info(
            f"Queued monthly compliance evidence refresh for {len(eligible)} subscribers "
            f"(skipped {len(missing_website)} without website)"
        )
    finally:
        db.close()


@celery_app.task(name="refresh_gebiz_base_rates")
def refresh_gebiz_base_rates():
    """
    4.5: Fetch GeBIZ Government Procurement Awards from data.gov.sg and
    update TenderShortlist.base_rate with real sector/agency win rates.

    Algorithm:
      base_rate = (unique vendors awarded in sector) / (total unique tenders in sector)
      clamped to [0.05, 0.60]

    Runs weekly via Celery Beat. Non-fatal — failures logged, no rollback needed.
    """
    import asyncio as _asyncio

    async def _fetch_and_update():
        from app.core.models import TenderShortlist
        from app.core.models import GebizAwardHistory
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from datetime import datetime as _dt
        db = SessionLocal()
        try:
            # ── 1. Fetch GeBIZ award data from data.gov.sg ──────────────────────
            # Dataset: "Government Procurement via GeBIZ" (MOF). Fields:
            #   tender_no, tender_description, agency, award_date (D/M/YYYY),
            #   tender_detail_status, supplier_name, awarded_amt
            # The two IDs used previously were stale (one 404s, the other was the
            # UEN company registry) so award_history never populated — see the
            # field mapping below. Verified live against the datastore API.
            GEBIZ_DATASET_IDS = [
                "d_acde1106003906a75c3fa052592f2fcb",  # Government Procurement via GeBIZ
            ]
            SECTOR_KEYWORDS = {
                "IT": ["information technology", "ict", "software", "hardware", "digital", "cyber", "data"],
                "CONSTRUCTION": ["construction", "building", "infrastructure", "civil"],
                "PROFESSIONAL_SERVICES": ["consultancy", "consulting", "professional services", "advisory"],
                "HEALTHCARE": ["healthcare", "health", "medical", "hospital"],
                "SECURITY": ["security", "surveillance", "guarding"],
                "FACILITIES": ["facilities", "maintenance", "cleaning", "property"],
                "LOGISTICS": ["logistics", "transport", "delivery", "freight"],
                "EDUCATION": ["education", "training", "learning"],
            }
            DEFAULT_BASE_RATE = 0.20
            CLAMP_MIN = 0.05
            CLAMP_MAX = 0.60
            PAGE_SIZE = 1000

            sector_awards: dict[str, int] = {}   # sector → awarded tender count
            sector_tenders: dict[str, int] = {}  # sector → total tenders seen
            agency_awards: dict[str, int] = {}   # agency → awarded count
            agency_tenders: dict[str, int] = {}  # agency → total seen

            # Politeness + resilience constants for the data.gov.sg API, which
            # rate-limits aggressively (HTTP 429). A single 429 used to abort the
            # entire refresh (it broke the loop and there is no fallback dataset);
            # now each page retries with exponential backoff and we pause between
            # pages so the weekly cron doesn't silently produce zero rows.
            MAX_PAGE_RETRIES = 4
            INTER_PAGE_DELAY = 0.6          # seconds between successful pages
            RETRYABLE_STATUS = {429, 500, 502, 503, 504}

            async def _fetch_page(client, dataset_id: str, offset: int):
                """GET one datastore page, retrying on 429 / 5xx / network errors
                with exponential backoff (honouring Retry-After). Returns parsed
                JSON, or None when the page can't be fetched after all retries."""
                backoff = 2.0
                for attempt in range(1, MAX_PAGE_RETRIES + 1):
                    try:
                        resp = await client.get(
                            "https://data.gov.sg/api/action/datastore_search",
                            params={"resource_id": dataset_id, "limit": PAGE_SIZE, "offset": offset},
                            headers={"User-Agent": "BooppaBot/1.0 (+https://www.booppa.io)"},
                        )
                    except Exception as e:
                        if attempt == MAX_PAGE_RETRIES:
                            logger.warning(
                                f"[GeBIZ] Network error dataset={dataset_id} offset={offset} "
                                f"after {attempt} tries: {e}"
                            )
                            return None
                        await _asyncio.sleep(backoff)
                        backoff *= 2
                        continue

                    if resp.status_code == 200:
                        return resp.json()

                    if resp.status_code in RETRYABLE_STATUS and attempt < MAX_PAGE_RETRIES:
                        ra = resp.headers.get("Retry-After")
                        try:
                            wait_s = float(ra) if ra else backoff
                        except (TypeError, ValueError):
                            wait_s = backoff
                        wait_s = min(wait_s, 30.0)
                        logger.info(
                            f"[GeBIZ] HTTP {resp.status_code} dataset={dataset_id} offset={offset} "
                            f"— retry {attempt}/{MAX_PAGE_RETRIES} in {wait_s:.0f}s"
                        )
                        await _asyncio.sleep(wait_s)
                        backoff *= 2
                        continue

                    # Non-retryable (e.g. 404 dead resource) or retries exhausted.
                    logger.warning(
                        f"[GeBIZ] HTTP {resp.status_code} dataset={dataset_id} offset={offset} — giving up"
                    )
                    return None
                return None

            fetched_any = False
            award_total = 0
            async with get_async_client(timeout=30.0) as client:
                for dataset_id in GEBIZ_DATASET_IDS:
                    offset = 0
                    while True:
                        data = await _fetch_page(client, dataset_id, offset)
                        if data is None:
                            # Page unfetchable after retries — stop paginating this
                            # dataset. Rows already committed below are retained.
                            break

                        records = data.get("result", {}).get("records", [])
                        if not records:
                            break

                        fetched_any = True
                        award_rows_batch: list[dict] = []
                        for rec in records:
                            # Normalise field names across dataset schema variants
                            description = (
                                rec.get("tender_description")
                                or rec.get("award_details")
                                or rec.get("description", "")
                            ).lower()
                            agency = (
                                rec.get("agency")
                                or rec.get("procuring_entity", "UNKNOWN")
                            ).upper().strip()
                            # A row counts as awarded when GeBIZ marked it
                            # "Awarded to Suppliers" / "Awarded by Items" (a named
                            # supplier exists). "Awarded to No Suppliers" rows have
                            # no supplier and are excluded from award_history.
                            status_desc = (rec.get("tender_detail_status") or "").lower()
                            awarded = bool(
                                (rec.get("supplier_name") or "").strip()
                                or ("awarded" in status_desc and "no supplier" not in status_desc)
                            )

                            # Classify sector from description keywords
                            matched_sector = "OTHER"
                            for sector, keywords in SECTOR_KEYWORDS.items():
                                if any(kw in description for kw in keywords):
                                    matched_sector = sector
                                    break

                            sector_tenders[matched_sector] = sector_tenders.get(matched_sector, 0) + 1
                            agency_tenders[agency] = agency_tenders.get(agency, 0) + 1
                            if awarded:
                                sector_awards[matched_sector] = sector_awards.get(matched_sector, 0) + 1
                                agency_awards[agency] = agency_awards.get(agency, 0) + 1

                                # Stash row for batch upsert into gebiz_award_history.
                                # Parse awarded_date defensively — data.gov.sg has used
                                # both "YYYY-MM-DD" and "DD/MM/YYYY" historically.
                                raw_date = rec.get("awarded_date") or rec.get("award_date")
                                parsed_date = None
                                if raw_date:
                                    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
                                        try:
                                            parsed_date = _dt.strptime(str(raw_date)[:10], fmt).date()
                                            break
                                        except (ValueError, TypeError):
                                            continue
                                amt_raw = (
                                    rec.get("awarded_amt")
                                    or rec.get("award_amt")
                                    or rec.get("award_amount")
                                )
                                try:
                                    amt = float(str(amt_raw).replace(",", "")) if amt_raw not in (None, "") else None
                                except (ValueError, TypeError):
                                    amt = None
                                award_rows_batch.append({
                                    "tender_no": (rec.get("tender_no") or rec.get("tender_no_") or "")[:100] or None,
                                    "awarded_date": parsed_date,
                                    "supplier_name": (rec.get("supplier_name") or "")[:255] or None,
                                    "award_amt": amt,
                                    "tender_description": rec.get("tender_description") or rec.get("award_details") or None,
                                    "procuring_entity": (rec.get("agency") or rec.get("procuring_entity") or "")[:255] or None,
                                    "sector": matched_sector,
                                    "raw": rec,
                                })

                        # Idempotent upsert: ON CONFLICT DO NOTHING via unique
                        # (tender_no, supplier_name, awarded_date) constraint.
                        if award_rows_batch:
                            try:
                                stmt = pg_insert(GebizAwardHistory).values(award_rows_batch)
                                stmt = stmt.on_conflict_do_nothing(
                                    constraint="uq_gebiz_award_history_tender_supplier_date"
                                )
                                db.execute(stmt)
                                db.commit()
                                award_total += len(award_rows_batch)
                            except Exception as e:
                                logger.warning(f"[GeBIZ] award_history upsert failed (offset={offset}): {e}")
                                db.rollback()

                        total = data.get("result", {}).get("total", 0)
                        offset += PAGE_SIZE
                        if offset >= total:
                            break
                        await _asyncio.sleep(INTER_PAGE_DELAY)  # be polite between pages

                    if fetched_any:
                        break  # got data from first working dataset

            if not fetched_any:
                logger.warning(
                    "[GeBIZ] No data fetched (all pages failed — likely rate-limited "
                    "after retries). base_rates and award_history unchanged"
                )
                return
            logger.info(f"[GeBIZ] Fetch complete — {award_total:,} award rows upserted (pre-dedupe)")

            # ── 2. Compute sector rates ─────────────────────────────────────────
            def _rate(awarded: int, total: int) -> float:
                if total == 0:
                    return DEFAULT_BASE_RATE
                return max(CLAMP_MIN, min(CLAMP_MAX, awarded / total))

            sector_rates = {
                s: _rate(sector_awards.get(s, 0), sector_tenders[s])
                for s in sector_tenders
            }
            agency_rates = {
                a: _rate(agency_awards.get(a, 0), agency_tenders[a])
                for a in agency_tenders
            }

            logger.info(f"[GeBIZ] Sector rates computed: {sector_rates}")
            logger.info(f"[GeBIZ] Top agency rates (sample): {dict(list(agency_rates.items())[:5])}")

            # ── 3. Update TenderShortlist.base_rate ─────────────────────────────
            tenders = db.query(TenderShortlist).all()
            updated = 0
            for tender in tenders:
                # Prefer agency-specific rate; fall back to sector rate; then default
                agency_key = (tender.agency or "").upper().strip()
                sector_key = (tender.sector or "OTHER").upper().strip()
                new_rate = (
                    agency_rates.get(agency_key)
                    or sector_rates.get(sector_key)
                    or DEFAULT_BASE_RATE
                )
                if abs(new_rate - tender.base_rate) > 0.005:
                    tender.base_rate = round(new_rate, 4)
                    updated += 1

            db.commit()
            logger.info(
                f"[GeBIZ] base_rate refresh complete: {updated}/{len(tenders)} tenders updated "
                f"from {sum(sector_tenders.values())} award records"
            )

        except Exception as e:
            logger.error(f"[GeBIZ] base_rate refresh failed: {e}")
            db.rollback()
        finally:
            db.close()

    _asyncio.run(_fetch_and_update())


@celery_app.task(name="sync_gebiz_tenders")
def sync_gebiz_tenders():
    """
    Fetch live GeBIZ open tenders via RSS (primary) then scrape the public
    listing (supplementary). Runs every 30 minutes via Celery Beat.
    Respects robots.txt: only public pages are accessed.

    Guarded by a short-lived Redis lock so overlapping runs can't stampede:
    if a queue backlog delivers many stranded beat ticks at once (as happened
    during the queue-name migration outage), only one runs at a time. Without
    this, a dozen concurrent runs each open Redis/HTTP/DB connections and
    exhaust the Redis client limit ("max number of clients reached").
    """
    from app.services.gebiz_service import fetch_from_rss, scrape_gebiz_page

    # Redis SETNX lock (auto-expires so a crashed run can't wedge the schedule
    # forever). The sync runs every 30 min; a 20-min TTL comfortably covers a
    # slow run while still self-healing well before the next scheduled tick.
    lock_key = "lock:sync_gebiz_tenders"
    redis_client = celery_app.backend.client
    try:
        got_lock = redis_client.set(lock_key, "1", nx=True, ex=1200)
    except Exception as lock_err:  # pragma: no cover - lock is best-effort
        logger.warning(f"[GeBIZ] lock acquire failed, running without lock: {lock_err}")
        got_lock = True

    if not got_lock:
        logger.info("[GeBIZ] sync already in progress; skipping this run")
        return

    db = SessionLocal()
    try:
        rss_count = fetch_from_rss(db)
        scrape_count = scrape_gebiz_page(db)
        logger.info(f"[GeBIZ] sync complete: rss={rss_count}, scrape={scrape_count}")

        # Bridge GebizTender → TenderShortlist so the probability engine can
        # score any RSS-synced tender without requiring a manual admin entry.
        _bridge_gebiz_to_shortlist(db)

        # Fire per-tender high-fit pushes (#4) for any tender ingested this run
        # that matches a buyer's watched-supplier sectors. Enqueued (not inline)
        # so a push failure never rolls back the sync; the ledger dedups.
        try:
            buyer_tender_fit_push_sweep_task.delay()
        except Exception as push_err:  # pragma: no cover
            logger.warning(f"[GeBIZ] tender push sweep enqueue failed: {push_err}")
    except Exception as exc:
        logger.error(f"[GeBIZ] sync_gebiz_tenders failed: {exc}")
        db.rollback()
    finally:
        db.close()
        try:
            redis_client.delete(lock_key)
        except Exception:  # pragma: no cover - lock will expire on its own
            pass


@celery_app.task(name="refresh_acra")
def refresh_acra():
    """
    Refresh the offline ACRA seed in `discovered_vendors` from the data.gov.sg
    business-entities dataset. Runs monthly via Celery Beat (the register is
    republished monthly). The live lookup in evidence_enricher handles any
    single entity on demand; this task just keeps the local seed warm so the
    Vendor Proof registry-match path can hit a row without a network call.

    Guarded by a Redis SETNX lock so a backlog of stranded beat ticks can't
    launch overlapping full-register pulls (mirrors sync_gebiz_tenders).
    """
    from app.services.acra_service import refresh_acra as _refresh_acra

    lock_key = "lock:refresh_acra"
    redis_client = celery_app.backend.client
    try:
        # Full pull can run for several minutes; 1h TTL self-heals a crash well
        # before the next monthly tick.
        got_lock = redis_client.set(lock_key, "1", nx=True, ex=3600)
    except Exception as lock_err:  # pragma: no cover - lock is best-effort
        logger.warning(f"[ACRA] lock acquire failed, running without lock: {lock_err}")
        got_lock = True

    if not got_lock:
        logger.info("[ACRA] refresh already in progress; skipping this run")
        return

    db = SessionLocal()
    try:
        count = _refresh_acra(db)
        logger.info(f"[ACRA] refresh_acra complete: {count} DiscoveredVendor rows upserted")
    except Exception as exc:
        logger.error(f"[ACRA] refresh_acra failed: {exc}")
        db.rollback()
    finally:
        db.close()
        try:
            redis_client.delete(lock_key)
        except Exception:  # pragma: no cover - lock will expire on its own
            pass


@celery_app.task(name="build_pdpc_precedent_index")
def build_pdpc_precedent_index():
    """
    Build the per-obligation PDPC enforcement precedent index from the live
    decisions register, so each finding type can cite REAL published decisions
    (the "PDPC enforcement precedents per finding" feature) instead of a static
    seed. Cached ~14 days; runs weekly via Celery Beat.

    Redis SETNX lock prevents overlapping runs (each run may fetch dozens of
    decision pages for fine/year enrichment).
    """
    from app.services.evidence_enricher import build_pdpc_precedent_index as _build

    lock_key = "lock:build_pdpc_precedent_index"
    redis_client = celery_app.backend.client
    try:
        got_lock = redis_client.set(lock_key, "1", nx=True, ex=3600)
    except Exception as lock_err:  # pragma: no cover - lock is best-effort
        logger.warning(f"[PDPC] index lock acquire failed, running without lock: {lock_err}")
        got_lock = True

    if not got_lock:
        logger.info("[PDPC] precedent index build already in progress; skipping")
        return

    try:
        index = asyncio.run(_build())
        logger.info(
            "[PDPC] precedent index build complete: %d decisions across %d categories",
            index.get("total", 0), len(index.get("categories") or {}),
        )
    except Exception as exc:
        logger.error(f"[PDPC] precedent index build failed: {exc}")
    finally:
        try:
            redis_client.delete(lock_key)
        except Exception:  # pragma: no cover - lock will expire on its own
            pass


# Consider the offline ACRA seed stale once it is older than this many days.
# The register republishes monthly, so ~25d self-heals a missed monthly tick
# while a routine same-week redeploy skips the multi-minute re-pull.
ACRA_SEED_STALE_DAYS = 25


@celery_app.task(name="bootstrap_reference_data")
def bootstrap_reference_data():
    """Fire a live pull of the reference datasets on worker boot (i.e. on deploy).

    The ACRA seed (`discovered_vendors`) and the PDPC precedent index otherwise
    only populate on their monthly / weekly Beat ticks, so a fresh deploy could
    serve an empty registry-match table and a missing precedent index for weeks.
    This runs once when the worker container starts (wired to the `worker_ready`
    signal in `celery_app.py`) and enqueues a refresh **only when the data is
    missing or stale**, so ordinary same-week redeploys don't re-pull the full
    register every time.

    A short Redis debounce lock keeps simultaneous replica boots from each
    enqueuing; the underlying refresh tasks also hold their own locks, so this
    is belt-and-braces. All checks are best-effort — a boot must never fail
    because a dataset probe raised.
    """
    redis_client = celery_app.backend.client
    try:
        # 10-minute debounce: one boot pull per deploy, not one per replica.
        got_lock = redis_client.set("lock:bootstrap_reference_data", "1", nx=True, ex=600)
    except Exception as lock_err:  # pragma: no cover - lock is best-effort
        logger.warning(f"[Bootstrap] lock acquire failed, proceeding: {lock_err}")
        got_lock = True
    if not got_lock:
        logger.info("[Bootstrap] reference-data pull already triggered this deploy; skipping")
        return

    # ── ACRA offline seed ────────────────────────────────────────────────────
    try:
        from app.core.models import DiscoveredVendor
        from sqlalchemy import func

        db = SessionLocal()
        try:
            row_count = (
                db.query(func.count(DiscoveredVendor.id))
                .filter(DiscoveredVendor.source == "acra")
                .scalar()
            ) or 0
            newest = (
                db.query(func.max(DiscoveredVendor.updated_at))
                .filter(DiscoveredVendor.source == "acra")
                .scalar()
            )
        finally:
            db.close()

        stale = (
            newest is None
            or (datetime.utcnow() - newest) > timedelta(days=ACRA_SEED_STALE_DAYS)
        )
        if row_count == 0 or stale:
            logger.info(
                "[Bootstrap] ACRA seed missing/stale (rows=%s, newest=%s) — enqueuing refresh_acra",
                row_count, newest,
            )
            refresh_acra.delay()
        else:
            logger.info(
                "[Bootstrap] ACRA seed fresh (rows=%s, newest=%s) — no pull needed",
                row_count, newest,
            )
    except Exception as exc:
        logger.warning(f"[Bootstrap] ACRA seed check failed (non-fatal): {exc}")

    # ── PDPC precedent index ─────────────────────────────────────────────────
    try:
        from app.services.evidence_enricher import load_pdpc_precedent_index

        if load_pdpc_precedent_index() is None:
            logger.info("[Bootstrap] PDPC precedent index absent — enqueuing build_pdpc_precedent_index")
            build_pdpc_precedent_index.delay()
        else:
            logger.info("[Bootstrap] PDPC precedent index present — no build needed")
    except Exception as exc:
        logger.warning(f"[Bootstrap] PDPC index check failed (non-fatal): {exc}")


def _bridge_gebiz_to_shortlist(db) -> None:
    """
    Upsert open GebizTenders into TenderShortlist with a default base_rate.
    Also writes GeBizActivity rows linking vendors to open tenders in their sector.
    """
    from app.core.models import GebizTender
    from app.core.models import TenderShortlist
    from app.core.models import GeBizActivity
    from app.core.models import VendorSector
    from app.services.tender_service import _CATEGORY_TO_SECTOR
    from datetime import datetime

    open_tenders = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .all()
    )

    # Build sector → [vendor_id] map once
    sector_vendor_map: dict = {}
    for sv in db.query(VendorSector).all():
        sector_vendor_map.setdefault(sv.sector, []).append(sv.vendor_id)

    # Existing GeBizActivity tender_ids to avoid duplicates
    existing_activities: set = set(
        row[0] for row in db.query(GeBizActivity.tender_id).all()
    )

    bridged = 0
    activities_added = 0
    for gt in open_tenders:
        raw = gt.raw_data or {}
        cat = raw.get("category", "")
        sector = _CATEGORY_TO_SECTOR.get(cat, "General")

        # ── TenderShortlist upsert ──────────────────────────────────────────
        existing = db.query(TenderShortlist).filter(
            TenderShortlist.tender_no == gt.tender_no
        ).first()
        if existing:
            existing.description = gt.title or existing.description
            existing.agency = gt.agency or existing.agency
        else:
            db.add(TenderShortlist(
                tender_no=gt.tender_no,
                description=gt.title,
                agency=gt.agency or "Government Agency",
                sector=sector,
                base_rate=0.20,
            ))
            bridged += 1

        # ── GeBizActivity: link matching vendors ────────────────────────────
        if gt.tender_no not in existing_activities:
            vendors_in_sector = sector_vendor_map.get(sector, [])
            for vendor_id in vendors_in_sector:
                db.add(GeBizActivity(
                    vendor_id=vendor_id,
                    tender_id=gt.tender_no,
                    domain=gt.agency or "gov.sg",
                    status="Open",
                    correlation_id=f"gebiz:{gt.tender_no}",
                    created_at=datetime.now(timezone.utc),
                ))
                activities_added += 1
            existing_activities.add(gt.tender_no)

    db.commit()
    if bridged:
        logger.info(f"[GeBIZ] Bridged {bridged} new tenders into TenderShortlist")
    if activities_added:
        logger.info(f"[GeBIZ] Created {activities_added} GeBizActivity rows for vendor sector matching")


@celery_app.task(bind=True, max_retries=2, name="scrape_vendor_contact_task")
def scrape_vendor_contact_task(self, vendor_id: str, model: str = "marketplace"):
    """Scrape a single vendor's website for contact emails."""
    from app.services.vendor_scraper import scrape_and_update_vendor

    db = SessionLocal()
    try:
        result = scrape_and_update_vendor(db, vendor_id, model=model)
        logger.info(f"[Scraper] vendor={vendor_id} result={result.get('status')} emails={result.get('email_count', 0)}")
        return result
    except Exception as exc:
        logger.error(f"[Scraper] Task failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=120 * (2 ** self.request.retries))
    finally:
        db.close()


@celery_app.task(name="scrape_vendor_contacts_batch")
def scrape_vendor_contacts_batch(model: str = "marketplace", limit: int = 50):
    """
    Batch scrape vendors missing contact emails.
    Queues individual tasks with staggered countdown to respect rate limits.
    Max 3 concurrent scrapes enforced by staggering.
    """
    db = SessionLocal()
    try:
        if model == "marketplace":
            from app.core.models import MarketplaceVendor as Model
        else:
            from app.core.models import DiscoveredVendor as Model

        from sqlalchemy import or_

        vendors = (
            db.query(Model)
            .filter(
                Model.contact_email.is_(None),
                or_(Model.domain.isnot(None), Model.website.isnot(None)),
                or_(Model.last_scraped_at.is_(None)),
            )
            .limit(limit)
            .all()
        )

        queued = 0
        for i, vendor in enumerate(vendors):
            # Stagger by 5 seconds per vendor to cap at ~3 concurrent
            scrape_vendor_contact_task.apply_async(
                args=[str(vendor.id), model],
                countdown=i * 5,
            )
            queued += 1

        logger.info(f"[Scraper] Batch queued {queued} vendors for scraping (model={model})")
        return {"queued": queued, "model": model}
    finally:
        db.close()


@celery_app.task(name="cleanup_old_tasks")
def cleanup_old_tasks():
    """Clean up old completed reports and temporary data"""
    db = SessionLocal()
    try:
        from app.core.models import AuditChainEvent

        # Delete reports older than 30 days
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)

        # Reports anchored to the audit chain are referenced by
        # audit_chain_events (a hash-chained, append-only evidence trail with a
        # non-cascading FK). Deleting them raises ForeignKeyViolation and, worse,
        # would sever the audit chain — so exclude any report that has audit
        # events. Only unanchored, purely-transient reports are pruned.
        anchored_ids = db.query(AuditChainEvent.report_id).distinct().subquery()

        old_reports = (
            db.query(Report)
            .filter(
                Report.status == "completed",
                Report.created_at < cutoff_date,
                ~Report.id.in_(db.query(anchored_ids.c.report_id)),
            )
            .all()
        )

        for report in old_reports:
            # In production, you might archive instead of delete
            db.delete(report)

        db.commit()
        logger.info(f"Cleaned up {len(old_reports)} old reports")

        # Prune the append-only search-impression log. The Vendor Active
        # snapshot only ever reads the trailing 30 days
        # (get_search_impressions_30d), so 90 days is a generous margin while
        # keeping the table from growing unbounded.
        from app.core.models import SearchImpression

        impression_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        deleted_impressions = (
            db.query(SearchImpression)
            .filter(SearchImpression.created_at < impression_cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        logger.info(f"Cleaned up {deleted_impressions} old search impressions")

    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed: {e}")
    finally:
        db.close()


@celery_app.task(name="sweep_pending_cover_sheets")
def sweep_pending_cover_sheets():
    """Backstop for Compliance Evidence Pack cover-sheet delivery.

    The cover sheet auto-fires inline from `_maybe_fire_cover_sheet` when both
    the PDPA scan and the RFP Complete kit finish. That inline trigger is
    correct in the happy path, but it runs from two async tasks and a missed
    fire (task ordering, a swallowed exception, a transient DB error) leaves the
    customer with `pending_cover_sheet=True` and NO cover sheet — silently, with
    no alert. A forensic audit caught exactly this: one buyer received the
    3-doc bundle's cover sheet, another (same SKU) never did.

    This hourly sweep re-runs the idempotent `_maybe_fire_cover_sheet` for every
    user still owed a cover sheet, so any transient miss self-heals within the
    hour. `_maybe_fire_cover_sheet` itself re-checks that both inputs are ready
    and clears the flag once it queues the task, so re-running it is safe and a
    no-op for buyers who simply haven't submitted their RFP brief yet.
    """
    from app.core.models import User
    from app.services.fulfillment import maybe_fire_cover_sheet

    db = SessionLocal()
    try:
        pending = (
            db.query(User)
            .filter(User.pending_cover_sheet == True)  # noqa: E712
            .all()
        )
        emails = [u.email for u in pending if u.email]
    except Exception as e:
        logger.error(f"[CoverSheetSweep] Could not list pending users: {e}")
        return
    finally:
        db.close()

    if not emails:
        return

    fired = 0
    for email in emails:
        try:
            # Idempotent: fires only when PDPA + RFP are both done, then clears
            # the flag. A no-op otherwise.
            maybe_fire_cover_sheet(email)
            fired += 1
        except Exception as e:
            logger.error(f"[CoverSheetSweep] Re-fire failed for {email}: {e}")
    logger.info(
        f"[CoverSheetSweep] Re-checked {len(emails)} pending cover-sheet user(s)"
    )


@celery_app.task(name="retry_failed_cover_sheet_anchors")
def retry_failed_cover_sheet_anchors():
    """Auto-recover signed Cover Sheet anchors stuck in 'Pending'.

    `anchor_signed_cover_sheet_task` retries 5× with backoff, then marks the
    Report `anchor_failed=True` with `tx_hash` still NULL. The status endpoint
    surfaces that flag so the UI can stop spinning — but until now nothing
    re-attempted the anchor, so a transient RPC/gas outage left the buyer's
    third bundle anchor permanently 'Pending' (a finding in the forensic audit).

    This hourly sweep re-queues `anchor_signed_cover_sheet_task` for any signed
    cover sheet that failed to anchor, bounded by `anchor_sweep_attempts` so a
    genuinely un-anchorable hash (bad config, contract revert) eventually stops
    retrying and is left for manual intervention via /bundle/cover-sheet/trigger.
    """
    from app.core.models import Report, User

    MAX_SWEEP_ATTEMPTS = 8  # ~8 hourly tries before giving up

    db = SessionLocal()
    requeued = 0
    try:
        candidates = (
            db.query(Report)
            .filter(
                Report.framework == "compliance_evidence_signed_sheet",
                Report.tx_hash.is_(None),
            )
            .all()
        )
        for r in candidates:
            src = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            if not src.get("anchor_failed"):
                continue  # still in normal retry window, not yet exhausted
            attempts = int(src.get("anchor_sweep_attempts", 0) or 0)
            if attempts >= MAX_SWEEP_ATTEMPTS:
                continue  # give up — leave anchor_failed for manual retry
            # Fresh dict so SQLAlchemy detects the JSONB change (reassigning the
            # same reference would not mark the column dirty).
            ad = {**src, "anchor_sweep_attempts": attempts + 1}
            r.assessment_data = ad
            owner = db.query(User).filter(User.id == r.owner_id).first()
            customer_email = owner.email if owner else None
            company_name = (getattr(owner, "company", "") or "") if owner else ""
            db.commit()
            anchor_signed_cover_sheet_task.apply_async(
                kwargs={
                    "report_id": str(r.id),
                    "customer_email": customer_email,
                    "company_name": company_name,
                },
                countdown=5,
            )
            requeued += 1
    except Exception as e:
        db.rollback()
        logger.error(f"[AnchorRetrySweep] Failed: {e}")
    finally:
        db.close()
    if requeued:
        logger.info(f"[AnchorRetrySweep] Re-queued {requeued} stuck cover-sheet anchor(s)")


@celery_app.task(name="send_weekly_vendor_scores")
def send_weekly_vendor_scores():
    """
    Send every active vendor their weekly compliance score summary.
    Runs every Monday at 08:00 UTC via Celery Beat.
    Non-fatal — individual email failures are logged and skipped.
    """
    from app.core.models import User
    from app.core.models import VendorScore

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        rows = (
            db.query(User, VendorScore)
            .join(VendorScore, VendorScore.vendor_id == User.id)
            .filter(User.is_active == True)
            .all()
        )
        email_svc = EmailService()
        for user, score in rows:
            try:
                subject = f"Your BOOPPA Vendor Score This Week — {score.total_score} pts"
                from app.services.email_layout import branded_email_html, email_button
                body_html = branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Your Weekly Vendor Score</h2>
                  <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hi {user.full_name or user.company or user.email},</p>
                  <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Here's how your BOOPPA compliance profile performed this week:</p>
                  <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                    <tr><td style="padding:8px 0;color:#64748b;">Compliance</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#2563eb;">{score.compliance_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#64748b;">Visibility</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#2563eb;">{score.visibility_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#64748b;">Engagement</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#2563eb;">{score.engagement_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#64748b;">Procurement Interest</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#2563eb;">{score.procurement_interest_score}</td></tr>
                    <tr style="border-top:1px solid #e2e8f0;">
                      <td style="padding:12px 0;color:#0f172a;font-weight:bold;">Total Score</td>
                      <td style="padding:12px 0;text-align:right;font-size:1.4em;font-weight:bold;color:#7c3aed;">{score.total_score}</td>
                    </tr>
                  </table>
                  {email_button("https://www.booppa.io/vendor/dashboard", "View Full Dashboard")}
                  <p style="margin:8px 0 0;font-size:12px;color:#94a3b8;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                    """,
                    title="Your Weekly Vendor Score",
                    preheader=f"Your BOOPPA vendor score this week — {score.total_score} pts.",
                )
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(user.email, subject, body_html, category="marketing"))
                sent += 1
            except Exception as exc:
                logger.warning(f"[WeeklyScore] Failed to send to {user.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[WeeklyScore] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[WeeklyScore] Sent={sent} Failed={failed}")


@celery_app.task(name="post_payment_drip")
def post_payment_drip(vendor_email: str, product_type: str = "",
                      company_name: str = "", report_id: str = ""):
    """
    D+1 drip email — queued with countdown=86400 (24h) from the Stripe webhook.
    Tells the vendor their 3 next steps to get value from their purchase.
    """
    import asyncio as _asyncio

    labels = {
        "vendor_proof": "Vendor Proof",
        "pdpa_quick_scan": "PDPA Snapshot",
        "rfp_express": "RFP Express",
        "rfp_complete": "RFP Complete",
        "compliance_notarization_1": "Notarization",
        "vendor_trust_pack": "Vendor Trust Pack",
        "rfp_accelerator": "RFP Accelerator",
        "enterprise_bid_kit": "Enterprise Bid Kit",
        "tender_intelligence_monthly": "Tender Intelligence",
        "tender_intelligence_annual": "Tender Intelligence",
        "vendor_pro_monthly": "Vendor Pro",
        "vendor_pro_annual": "Vendor Pro",
    }
    label = labels.get(product_type, "your BOOPPA product")
    name = company_name or vendor_email

    from app.services.email_layout import branded_email_html, email_button
    body_html = branded_email_html(
        f"""
      <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">What to do next with your {label}</h2>
      <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello {name},</p>
      <ol style="line-height:1.9;color:#334155;font-size:15px;padding-left:20px;margin:0 0 20px;">
        <li><strong>Add your QR badge to your email signature.</strong>
            Every email is a buyer touchpoint.</li>
        <li><strong>Check your sector percentile</strong> on your dashboard.
            Below median = add notarized documents.</li>
        <li><strong>Run the Tender Win Calculator</strong> at
            booppa.io/tender-check to see your exact win probability.</li>
      </ol>
      {email_button("https://www.booppa.io/vendor/dashboard", "Go to your dashboard")}
      <p style="color:#64748b;font-size:12px;margin:8px 0 0;">booppa.io</p>
        """,
        title=f"What to do next with your {label}",
        preheader=f"3 steps to get the most from your {label}.",
    )

    try:
        email_svc = EmailService()
        _asyncio.run(email_svc.send_html_email(
            to_email=vendor_email,
            subject=f"What to do next with your {label} — 3 steps",
            body_html=body_html,
        ))
        logger.info(f"[PostPaymentDrip] Sent to {vendor_email} product={product_type}")
    except Exception as exc:
        logger.error(f"[PostPaymentDrip] Failed for {vendor_email}: {exc}")


@celery_app.task(name="send_gebiz_alert_newsletter")
def send_gebiz_alert_newsletter():
    """
    Send every active vendor a curated list of GeBIZ tenders closing within 14 days.
    Runs every Monday at 07:00 UTC via Celery Beat (one hour before the score digest).
    Non-fatal — individual email failures are logged and skipped.
    """
    from app.core.models import User
    from app.core.models import GebizTender
    from datetime import timedelta

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(days=14)

        tenders = (
            db.query(GebizTender)
            .filter(
                GebizTender.status == "Open",
                GebizTender.closing_date >= now,
                GebizTender.closing_date <= deadline,
            )
            .order_by(GebizTender.closing_date.asc())
            .limit(10)
            .all()
        )

        if not tenders:
            logger.info("[GeBIZAlert] No tenders closing within 14 days — skipping newsletter")
            return

        # Build the tender rows HTML once, reuse per vendor
        rows_html = ""
        for t in tenders:
            days_left = (t.closing_date - now).days if t.closing_date else "?"
            value_str = f"S${t.estimated_value:,.0f}" if t.estimated_value else "Not disclosed"
            tender_url = t.url or f"https://www.gebiz.gov.sg"
            rows_html += f"""
            <tr>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#0f172a;">
                <a href="{tender_url}" style="color:#7c3aed;text-decoration:none;font-weight:500;">{t.tender_no}</a><br>
                <span style="font-size:0.85em;color:#64748b;">{t.title[:120]}</span>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#64748b;white-space:nowrap;">{t.agency}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#2563eb;white-space:nowrap;">{value_str}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;white-space:nowrap;">
                <span style="color:{'#ef4444' if isinstance(days_left, int) and days_left <= 3 else '#d97706' if isinstance(days_left, int) and days_left <= 7 else '#059669'}">
                  {days_left}d left
                </span>
              </td>
            </tr>"""

        vendors = db.query(User).filter(User.is_active == True).all()
        email_svc = EmailService()

        for vendor in vendors:
            try:
                subject = f"GeBIZ Alert: {len(tenders)} tenders closing in the next 14 days"
                from app.services.email_layout import branded_email_html, email_button
                body_html = branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Tenders Closing Soon</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    Hi {vendor.full_name or vendor.company or vendor.email},<br>
                    Here are the GeBIZ opportunities closing within the next 14 days.
                    Check your win probability before you bid.
                  </p>

                  <table style="width:100%;border-collapse:collapse;margin:20px 0;font-size:0.9em;">
                    <thead>
                      <tr style="border-bottom:1px solid #e2e8f0;">
                        <th style="padding:8px;text-align:left;color:#64748b;font-weight:600;">Tender</th>
                        <th style="padding:8px;text-align:left;color:#64748b;font-weight:600;">Agency</th>
                        <th style="padding:8px;text-align:left;color:#64748b;font-weight:600;">Est. Value</th>
                        <th style="padding:8px;text-align:left;color:#64748b;font-weight:600;">Deadline</th>
                      </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                  </table>

                  {email_button("https://www.booppa.io/tender-check", "Check Win Probability →")}
                  {email_button("https://www.booppa.io/opportunities", "View All Open Tenders", primary=False)}

                  <p style="margin:8px 0 0;font-size:12px;color:#94a3b8;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                    """,
                    title="GeBIZ · Tenders closing soon",
                    preheader=f"{len(tenders)} GeBIZ tenders closing in the next 14 days.",
                )
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(vendor.email, subject, body_html, category="marketing"))
                sent += 1
            except Exception as exc:
                logger.warning(f"[GeBIZAlert] Failed to send to {vendor.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[GeBIZAlert] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[GeBIZAlert] Tenders={len(tenders)} Sent={sent} Failed={failed}")


@celery_app.task(name="send_tender_alerts")
def send_tender_alerts():
    """Daily BID-tender alert email for Tender Intelligence subscribers.

    For each subscriber, classify the live open tenders closing within the alert
    horizon using the SAME classifier as the in-app feed + monthly digest, and
    email any *new* BID-rated tenders they haven't already been alerted to. The
    `vendor_tender_alerts_sent` ledger provides dedup (GeBIZ tenders carry no
    creation timestamp), so an open tender is emailed at most once per vendor.
    """
    from app.billing.enforcement import TENDER_INTELLIGENCE_PLAN_KEYS
    from app.core.models import User, Subscription as SubModel
    from app.core.models import GebizTender
    from app.core.models import VendorSector
    from app.core.models import VendorTenderAlertSent
    from app.services.tender_service_bid_classifier import build_vendor_history, classify_tender
    from datetime import timedelta
    import asyncio as _asyncio

    _ALERT_MIN_DAYS = 5    # too close to prepare a quality bid → skip
    _ALERT_MAX_DAYS = 30   # only surface tenders within a month of closing

    db = SessionLocal()
    sent = 0
    try:
        active = db.query(SubModel).filter(
            SubModel.product_type.in_(list(TENDER_INTELLIGENCE_PLAN_KEYS)),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active if s.user_id}
        if not user_ids:
            logger.info("[TenderAlerts] no active Tender Intelligence subscribers — skipping")
            return
        subscribers = db.query(User).filter(User.id.in_(user_ids)).all()

        now = datetime.now(timezone.utc)
        lo = now + timedelta(days=_ALERT_MIN_DAYS)
        hi = now + timedelta(days=_ALERT_MAX_DAYS)
        
        # We query tenders inside the subscriber loop to filter by sector.
        _base_tender_q = db.query(GebizTender).filter(
            GebizTender.status == "Open",
            GebizTender.closing_date >= lo,
            GebizTender.closing_date <= hi,
        )

        def _tdict(t):
            return {
                "tender_no": t.tender_no, "title": t.title, "agency": t.agency,
                "closing_date": t.closing_date, "estimated_value": t.estimated_value,
                "sector": getattr(t, "sector", None), "status": t.status, "url": t.url,
            }

        email_svc = EmailService()
        for sub in subscribers:
            if not sub.email:
                continue
            try:
                sec = db.query(VendorSector).filter(VendorSector.vendor_id == sub.id).first()
                sector = (sec.sector.upper() if sec and sec.sector else "IT")
                history = build_vendor_history(db, str(sub.id), sector=sector)

                from app.services.tender_service import _CATEGORY_TO_SECTOR
                matching_categories = [c for c, sc in _CATEGORY_TO_SECTOR.items() if sc.lower() == sector.lower()]
                
                if matching_categories:
                    live = _base_tender_q.filter(
                        GebizTender.raw_data['category'].astext.in_(matching_categories)
                    ).order_by(GebizTender.closing_date.desc()).limit(50).all()
                else:
                    live = _base_tender_q.order_by(GebizTender.closing_date.desc()).limit(50).all()
                    
                if not live:
                    continue

                already = {
                    r[0] for r in db.query(VendorTenderAlertSent.tender_no)
                    .filter(VendorTenderAlertSent.vendor_id == sub.id).all()
                }
                new_bids = []
                for t in live:
                    if t.tender_no in already:
                        continue
                    c = classify_tender(_tdict(t), history)
                    if c.get("label") == "BID":
                        new_bids.append((t, c))
                if not new_bids:
                    continue

                rows_html = "".join(
                    f'<li style="margin-bottom:10px;"><strong>{(t.title or t.tender_no)}</strong>'
                    f'<br><span style="color:#475569;font-size:13px;">{(t.agency or "")} · closes '
                    f'{t.closing_date:%d %b %Y}</span>'
                    f'<br><span style="color:#16a34a;font-size:13px;">BID — {c.get("reason","")}</span>'
                    + (f'<br><a href="{t.url}" style="color:#7c3aed;font-size:13px;">View on GeBIZ</a>' if t.url else "")
                    + "</li>"
                    for t, c in new_bids
                )
                body_html = (
                    '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#0f172a;">'
                    '<h2 style="color:#7c3aed;">New tenders worth bidding on</h2>'
                    f'<p>{len(new_bids)} new GeBIZ tender(s) match your profile and are rated <strong>BID</strong>:</p>'
                    f'<ul style="padding-left:18px;">{rows_html}</ul>'
                    '<p style="color:#64748b;font-size:12px;">BID/WATCH/PASS is a rule-based estimate from sector '
                    'averages and your declared history — not a guarantee. Manage these in your '
                    '<a href="https://www.booppa.io/vendor/tender-intelligence" style="color:#7c3aed;">Tender Intelligence dashboard</a>.</p>'
                    "</div>"
                )
                subject = f"{len(new_bids)} new tender(s) to bid on — Booppa Tender Intelligence"
                ok = _asyncio.run(email_svc.send_html_email(sub.email, subject, body_html, category="marketing"))
                if ok:
                    for t, _c in new_bids:
                        db.add(VendorTenderAlertSent(vendor_id=sub.id, tender_no=t.tender_no))
                    db.commit()
                    sent += 1
            except Exception as exc:  # per-recipient isolation
                db.rollback()
                logger.warning("[TenderAlerts] failed for %s: %s", sub.id, exc)
        logger.info("[TenderAlerts] alert emails sent to %d subscriber(s)", sent)
    finally:
        db.close()


@celery_app.task(name="send_tender_intelligence_digest")
def send_tender_intelligence_digest(target_user_id: str | None = None):
    """
    Tender Intelligence digest — sector trend summary over the past 30 days.

    Called two ways:
      • From the daily anniversary cron with no args → sends to every active
        subscriber whose anniversary day matches today.
      • From `send_tender_intelligence_digest_for_user(user_id)` with
        target_user_id set → sends to just that one user (for instant first-
        cycle delivery on subscription activation).

    Only sends to users with an active subscription whose plan is in
    TENDER_INTELLIGENCE_PLAN_KEYS. Non-fatal — failures per recipient logged.
    """
    from app.core.models import User
    from app.core.models import GebizAwardHistory
    from app.billing.enforcement import TENDER_INTELLIGENCE_PLAN_KEYS
    from sqlalchemy import func  # noqa: F401  (used elsewhere in this file via late binding)
    from datetime import timedelta
    import asyncio as _asyncio
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from app.services.pdf_logo import draw_logo_header

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        # GeBIZ awards are published to data.gov.sg with a long lag (often
        # ~12 months), so a "last 30 days from today" window is almost always
        # empty. Anchor the window on the most recent award date we actually
        # have and report the trailing WINDOW_DAYS of available data.
        WINDOW_DAYS = 90
        latest_award = db.query(func.max(GebizAwardHistory.awarded_date)).scalar()
        if latest_award is None:
            logger.info(
                "[TenderIntelDigest] gebiz_award_history is empty — skipping send "
                "(run refresh_gebiz_base_rates to populate)"
            )
            return
        since = latest_award - timedelta(days=WINDOW_DAYS)
        rows = (
            db.query(GebizAwardHistory)
            .filter(
                GebizAwardHistory.awarded_date != None,  # noqa: E711
                GebizAwardHistory.awarded_date >= since,
            )
            .all()
        )

        if not rows:
            logger.info(
                "[TenderIntelDigest] No award rows in trailing %dd window "
                "(latest award %s) — skipping send", WINDOW_DAYS, latest_award,
            )
            return

        # Human-readable label for the actual window covered (data lags, so this
        # is not "this month" — it's the latest WINDOW_DAYS of published awards).
        period_label = (
            f"{since:%b %Y} – {latest_award:%b %Y}"
            if since.strftime("%Y-%m") != latest_award.strftime("%Y-%m")
            else f"{latest_award:%b %Y}"
        )

        # Aggregate top sectors and agencies by award count + total value.
        import statistics as _stats

        def _fmt_month(mk: str) -> str:
            try:
                return datetime.strptime(mk, "%Y-%m").strftime("%b %Y")
            except Exception:
                return mk

        # Skip rows with no real supplier — null/empty/"UNKNOWN" rows otherwise
        # surface as a top supplier with S$0 awards and destroy the table's
        # credibility (forensic-audit finding: "UNKNOWN — 24 wins — S$0").
        _BAD_SUPPLIERS = {"", "UNKNOWN", "UNDISCLOSED", "N/A", "NA", "NULL", "-"}

        def _aggregate(award_rows: list) -> dict:
            """All digest stats for a set of award rows. Pure aggregation so it can
            be run once market-wide and again per-subscriber for their sector."""
            sector_stats: dict[str, dict] = {}
            agency_stats: dict[str, dict] = {}
            supplier_stats: dict[str, dict] = {}
            month_stats: dict[str, dict] = {}
            sector_amounts: dict[str, list] = {}
            total = 0.0
            for r in award_rows:
                amt = float(r.award_amt) if r.award_amt is not None else 0.0
                total += amt
                sec = (r.sector or "OTHER").upper()
                ag = (r.procuring_entity or "UNKNOWN").upper()
                for bucket, key in ((sector_stats, sec), (agency_stats, ag)):
                    e = bucket.setdefault(key, {"count": 0, "value": 0.0})
                    e["count"] += 1
                    e["value"] += amt
                # Supplier benchmarking — real awardees only.
                sup = (r.supplier_name or "").strip().upper()
                if sup not in _BAD_SUPPLIERS:
                    se = supplier_stats.setdefault(sup, {"count": 0, "value": 0.0})
                    se["count"] += 1
                    se["value"] += amt
                if r.awarded_date:
                    mk = r.awarded_date.strftime("%Y-%m")
                    me = month_stats.setdefault(mk, {"count": 0, "value": 0.0})
                    me["count"] += 1
                    me["value"] += amt
                if amt > 0:
                    sector_amounts.setdefault(sec, []).append(amt)

            top_sectors = sorted(sector_stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
            top_agencies = sorted(agency_stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
            top_suppliers = sorted(supplier_stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
            price_by_sector = []
            for _sec_name, _v in top_sectors:
                _amts = sorted(sector_amounts.get(_sec_name, []))
                if _amts:
                    price_by_sector.append((_sec_name, _amts[0], _stats.median(_amts), _amts[-1]))
            timing_rows = sorted(month_stats.items(), key=lambda kv: kv[0])
            busiest_month = (
                max(month_stats.items(), key=lambda kv: kv[1]["count"])[0]
                if month_stats else None
            )
            return {
                "count": len(award_rows),
                "total_value": total,
                "top_sectors": top_sectors,
                "top_agencies": top_agencies,
                "top_suppliers": top_suppliers,
                "price_by_sector": price_by_sector,
                "timing_rows": timing_rows,
                "busiest_label": _fmt_month(busiest_month) if busiest_month else None,
            }

        # ── Render + upload the digest PDF for a given aggregation/scope ──────
        def _render_pdf(agg: dict, scope_label: str) -> str | None:
            try:
                buf = BytesIO()
                doc = SimpleDocTemplate(
                    buf, pagesize=A4,
                    leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                    topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                )
                styles = get_unified_styles()
                h_style = ParagraphStyle(
                    "h", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor("#0f172a"),
                    spaceAfter=6,
                )
                sub_style = ParagraphStyle(
                    "sub", parent=styles["Normal"], fontSize=10,
                    textColor=colors.HexColor("#64748b"), spaceAfter=18,
                )
                h2_style = ParagraphStyle(
                    "h2", parent=styles["Heading2"], fontSize=12,
                    textColor=colors.HexColor("#0f172a"), spaceAfter=8, spaceBefore=14,
                )
                body_style = ParagraphStyle(
                    "body", parent=styles["Normal"], fontSize=10,
                    textColor=colors.HexColor("#0f172a"), spaceAfter=6,
                )

                story: list = []
                story.append(Paragraph("Tender Intelligence — Monthly Digest", h_style))
                story.append(Paragraph(
                    f"{period_label} · {scope_label} · {agg['count']} awards · "
                    f"Total value S${agg['total_value']:,.0f}",
                    sub_style,
                ))

                def _build_table(rows_data: list[list], header: list[str], colWidths=None) -> Table:
                    t = Table([header] + rows_data, hAlign="LEFT", colWidths=colWidths or [3.2 * inch, 1.2 * inch, 1.8 * inch])
                    t.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cbd5e1")),
                        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ]))
                    return t

                story.append(Paragraph("Top sectors", h2_style))
                story.append(_build_table(
                    [[k, str(v["count"]), f"S${v['value']:,.0f}"] for k, v in agg["top_sectors"]],
                    ["Sector", "Awards", "Total value"],
                ))

                story.append(Paragraph("Top procuring entities", h2_style))
                story.append(_build_table(
                    [[k, str(v["count"]), f"S${v['value']:,.0f}"] for k, v in agg["top_agencies"]],
                    ["Agency", "Awards", "Total value"],
                ))

                if agg["top_suppliers"]:
                    story.append(Paragraph("Top suppliers — who's winning", h2_style))
                    story.append(_build_table(
                        [[k[:38], str(v["count"]),
                          f"S${(v['value'] / v['count'] if v['count'] else 0):,.0f}",
                          f"S${v['value']:,.0f}"]
                         for k, v in agg["top_suppliers"]],
                        ["Supplier", "Wins", "Avg award", "Total"],
                        colWidths=[2.7 * inch, 0.8 * inch, 1.4 * inch, 1.3 * inch],
                    ))

                if agg["price_by_sector"]:
                    story.append(Paragraph("Typical contract size by sector", h2_style))
                    story.append(_build_table(
                        [[s[:30], f"S${lo:,.0f}", f"S${med:,.0f}", f"S${hi:,.0f}"]
                         for s, lo, med, hi in agg["price_by_sector"]],
                        ["Sector", "Low", "Median", "High"],
                        colWidths=[2.7 * inch, 1.1 * inch, 1.2 * inch, 1.2 * inch],
                    ))

                if agg["timing_rows"]:
                    story.append(Paragraph("When awards land — bid timing", h2_style))
                    if agg["busiest_label"]:
                        story.append(Paragraph(
                            f"Busiest award month in this window: <b>{agg['busiest_label']}</b>. "
                            "Line up submissions to land ahead of peak procurement cycles.",
                            body_style,
                        ))
                    story.append(_build_table(
                        [[_fmt_month(mk), str(v["count"]), f"S${v['value']:,.0f}"]
                         for mk, v in agg["timing_rows"]],
                        ["Month", "Awards", "Value"],
                    ))

                story.append(Spacer(1, 0.3 * inch))
                story.append(Paragraph(
                    "Source: GeBIZ / data.gov.sg Government Procurement Awards. "
                    "Supplier benchmarking, contract-size bands, and bid-timing are "
                    "computed from the published award history for the window shown. "
                    "Open the live dashboard for current-tender bid/watch/pass signals.",
                    body_style,
                ))

                doc.build(story, onFirstPage=draw_logo_header, onLaterPages=draw_logo_header)
                pdf_bytes = buf.getvalue()

                from app.services.storage import S3Service
                s3 = S3Service()
                _scope_slug = "".join(c for c in scope_label.lower() if c.isalnum()) or "all"
                digest_id = f"tender-intel-digest-{since.strftime('%Y-%m')}-{_scope_slug}"
                url = _asyncio.run(s3.upload_pdf(pdf_bytes, digest_id))
                logger.info(f"[TenderIntelDigest] PDF uploaded: {digest_id}")
                return url
            except Exception as exc:
                logger.warning(f"[TenderIntelDigest] PDF generation/upload failed: {exc} — falling back to email-only")
                return None

        def _row(label: str, e: dict) -> str:
            return f"""
            <tr>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#0f172a;">{label}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#2563eb;text-align:right;">{e['count']}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#64748b;text-align:right;">S${e['value']:,.0f}</td>
            </tr>"""

        def _row4(c0, c1, c2, c3) -> str:
            return f"""
            <tr>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#0f172a;">{c0}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#2563eb;text-align:right;">{c1}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#64748b;text-align:right;">{c2}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#64748b;text-align:right;">{c3}</td>
            </tr>"""

        def _email_section(title: str, header_cols: list[str], rows_html: str) -> str:
            if not rows_html:
                return ""
            ths = "".join(
                f'<th style="padding:8px;text-align:{"left" if i == 0 else "right"};color:#64748b;">{h}</th>'
                for i, h in enumerate(header_cols)
            )
            return f"""
                  <h3 style="color:#0f172a;margin-top:32px;font-size:1.05em;">{title}</h3>
                  <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
                    <thead><tr style="border-bottom:1px solid #e2e8f0;">{ths}</tr></thead>
                    <tbody>{rows_html}</tbody>
                  </table>"""

        # ── Email HTML sections for a given aggregation ──────────────────────
        def _render_email_sections(agg: dict) -> tuple[str, str, str]:
            sector_rows = "".join(_row(k, v) for k, v in agg["top_sectors"])
            agency_rows = "".join(_row(k, v) for k, v in agg["top_agencies"])
            supplier_rows_html = "".join(
                _row4(k[:38], v["count"],
                      f"S${(v['value'] / v['count'] if v['count'] else 0):,.0f}",
                      f"S${v['value']:,.0f}")
                for k, v in agg["top_suppliers"]
            )
            price_rows_html = "".join(
                _row4(s[:30], f"S${lo:,.0f}", f"S${med:,.0f}", f"S${hi:,.0f}")
                for s, lo, med, hi in agg["price_by_sector"]
            )
            timing_rows_html = "".join(_row(_fmt_month(mk), v) for mk, v in agg["timing_rows"])

            extra = ""
            if agg["busiest_label"]:
                extra += (
                    f'<p style="color:#334155;margin-top:32px;">⏱ '
                    f'<strong style="color:#0f172a;">Bid timing:</strong> the busiest award month in this '
                    f'window was <strong style="color:#2563eb;">{agg["busiest_label"]}</strong> — plan submissions '
                    f'to land ahead of peak procurement cycles.</p>'
                )
            extra += _email_section(
                "Top suppliers — who's winning", ["Supplier", "Wins", "Avg award", "Total"], supplier_rows_html)
            extra += _email_section(
                "Typical contract size by sector", ["Sector", "Low", "Median", "High"], price_rows_html)
            extra += _email_section(
                "Awards by month", ["Month", "Awards", "Value"], timing_rows_html)
            return sector_rows, agency_rows, extra

        sub_query = db.query(User).filter(
            User.is_active == True,  # noqa: E712
            func.lower(User.plan).in_([p.lower() for p in TENDER_INTELLIGENCE_PLAN_KEYS]),
        )
        if target_user_id:
            sub_query = sub_query.filter(User.id == target_user_id)
        else:
            # Anniversary-day filter (short-month aware).
            sub_query = sub_query.filter(
                _anniversary_match_filter(User.subscription_anniversary_day)
            )
        subscribers = sub_query.all()

        # ── BID/WATCH/PASS — Block A (once per run) ──────────────────────────
        # Fetching of live tenders has been moved to Block B (inside the
        # subscriber loop) to ensure strict per-subscriber sector filtering.
        from app.core.models import GebizTender
        from app.core.models import VendorSector
        from app.services.tender_service_bid_classifier import (
            build_vendor_history,
            enrich_tender_digest_with_classifications,
            bid_label_to_html_badge,
        )



        if not subscribers:
            logger.info(
                "[TenderIntelDigest] No matching subscribers (target=%s) — skipping send",
                target_user_id or "anniversary-cron",
            )
            return

        email_svc = EmailService()
        period = period_label

        # Per-subscriber sector-scoped digest, cached by sector signature so we
        # aggregate + render at most once per distinct sector set. An IT vendor
        # must not receive a digest dominated by Facilities/Construction awards
        # (forensic-audit finding: sector filter not applied).
        _digest_cache: dict[tuple, tuple] = {}

        def _digest_for(sectors: list[str]):
            key = tuple(sorted(s.lower() for s in sectors)) if sectors else ()
            if key in _digest_cache:
                return _digest_cache[key]
            if key:
                subset = [r for r in rows if (r.sector or "").strip().lower() in key]
                if subset:
                    scope = f"{sectors[0].title()} sector"
                else:  # vendor's sector has no awards in window — show full market
                    subset, scope = rows, "all sectors"
            else:
                subset, scope = rows, "all sectors"
            agg = _aggregate(subset)
            result = (agg, _render_pdf(agg, scope), _render_email_sections(agg), scope)
            _digest_cache[key] = result
            return result

        for sub in subscribers:
            try:
                # ── BID/WATCH/PASS — Block B (per subscriber) ────────────────
                # THIS subscriber's sector + history. Must stay inside the loop —
                # hoisting it out is the v1 cross-contamination bug (every vendor
                # got the IT-sector recommendation).
                _sub_sectors = [
                    (r.sector or "").strip()
                    for r in db.query(VendorSector).filter(VendorSector.vendor_id == sub.id).all()
                    if (r.sector or "").strip()
                ]
                _sub_sector = (_sub_sectors[0].upper() if _sub_sectors else "IT")

                # Sector-scoped benchmarking digest for this subscriber.
                _agg, pdf_url, (sector_rows, agency_rows, extra_sections_html), _scope = _digest_for(_sub_sectors)
                total_value = _agg["total_value"]
                _award_count = _agg["count"]
                subject = f"Tender Intelligence — {_award_count} GeBIZ awards ({period_label})"

                from app.services.tender_service import _CATEGORY_TO_SECTOR
                matching_categories = []
                for s in _sub_sectors:
                    matching_categories.extend([c for c, sc in _CATEGORY_TO_SECTOR.items() if sc.lower() == s.lower()])
                
                # Fetch live tenders specifically for this vendor's sector
                _base_q = db.query(GebizTender).filter(
                    GebizTender.status == "Open",
                    GebizTender.closing_date >= datetime.now(timezone.utc),
                    GebizTender.estimated_value > 0,  # Ensure value column is never blank
                )
                
                if matching_categories:
                    _sub_tenders = (
                        _base_q.filter(GebizTender.raw_data['category'].astext.in_(matching_categories))
                        .order_by(GebizTender.closing_date.desc())  # Prioritize comfortable deadlines for better AI scoring
                        .limit(10)
                        .all()
                    )
                else:
                    _sub_tenders = _base_q.order_by(GebizTender.closing_date.desc()).limit(10).all()
                    
                # Pad with generic tenders if sector-specific count is low
                if len(_sub_tenders) < 10:
                    _existing_ids = {t.id for t in _sub_tenders}
                    _pad = _base_q.filter(~GebizTender.id.in_(_existing_ids)).order_by(GebizTender.closing_date.desc()).limit(10 - len(_sub_tenders)).all()
                    _sub_tenders.extend(_pad)

                _live_tender_dicts = [
                    {
                        "tender_no": t.tender_no,
                        "title": t.title,
                        "agency": t.agency,
                        "closing_date": t.closing_date,
                        "estimated_value": t.estimated_value,
                        "sector": getattr(t, "sector", None),
                        "status": t.status,
                        "url": t.url,
                    }
                    for t in _sub_tenders
                ]

                _vendor_history = build_vendor_history(db, str(sub.id), sector=_sub_sector)
                _classified_tenders = enrich_tender_digest_with_classifications(
                    _live_tender_dicts, vendor_history=_vendor_history,
                )

                disclaimer_placeholder = (
                    '<p style="font-size:11px;color:#94a3b8;margin-top:6px;">'
                    'BID/WATCH/PASS is a rule-based estimate using sector averages and your '
                    'self-reported history (if provided at signup) — not a guarantee of '
                    'outcome. <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">'
                    'Update your win-rate history</a> to improve accuracy.</p>'
                )

                bid_rows_html = ""
                for _t in _classified_tenders:
                    _badge = bid_label_to_html_badge(_t["bid_label"])
                    _close_str = _t["closing_date"].strftime("%d %b") if _t.get("closing_date") else "—"
                    _val_str = f"S${_t['estimated_value']:,.0f}" if _t.get("estimated_value") else "—"
                    _tender_url = _t.get("url") or "https://www.gebiz.gov.sg"
                    _title_cell = f'<a href="{_tender_url}" style="color:#7c3aed;">{(_t.get("title") or "")[:60]}</a>'
                    bid_rows_html += f"""
                        <tr>
                          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:13px;color:#0f172a;">{_title_cell}</td>
                          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#64748b;">{(_t.get("agency") or "")[:20]}</td>
                          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right;color:#0f172a;">{_val_str}</td>
                          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right;color:#0f172a;">{_close_str}</td>
                          <td style="padding:8px;border-bottom:1px solid #e2e8f0;text-align:center;">{_badge}</td>
                        </tr>"""

                bid_table_html = "" if not bid_rows_html else f"""
                  <h3 style="color:#0f172a;margin-top:32px;font-size:1.05em;">Live tenders — your recommendation</h3>
                  <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
                    <thead><tr style="border-bottom:1px solid #e2e8f0;">
                      <th style="padding:8px;text-align:left;color:#64748b;">Tender</th>
                      <th style="padding:8px;text-align:left;color:#64748b;">Agency</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Value</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Closes</th>
                      <th style="padding:8px;text-align:center;color:#64748b;">Action</th>
                    </tr></thead>
                    <tbody>{bid_rows_html}</tbody>
                  </table>
                  <p style="font-size:12px;color:#94a3b8;">BID = strong fit · WATCH = monitor · PASS = skip this cycle</p>
                  {disclaimer_placeholder}"""

                from app.services.email_layout import branded_email_html, email_button
                _pdf_btn = email_button(pdf_url, "Download PDF report", primary=False) if pdf_url else ""
                body_html = branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Monthly Sector Trends — {period}</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    Hi {sub.full_name or sub.company or sub.email},<br>
                    Across {period_label} ({_scope}), GeBIZ awarded <strong style="color:#0f172a;">{_award_count} contracts</strong>
                    totalling <strong style="color:#2563eb;">S${total_value:,.0f}</strong>.
                  </p>

                  <h3 style="color:#0f172a;margin-top:32px;font-size:1.05em;">Top sectors</h3>
                  <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
                    <thead><tr style="border-bottom:1px solid #e2e8f0;">
                      <th style="padding:8px;text-align:left;color:#64748b;">Sector</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Awards</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Total value</th>
                    </tr></thead>
                    <tbody>{sector_rows}</tbody>
                  </table>

                  <h3 style="color:#0f172a;margin-top:32px;font-size:1.05em;">Top procuring entities</h3>
                  <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
                    <thead><tr style="border-bottom:1px solid #e2e8f0;">
                      <th style="padding:8px;text-align:left;color:#64748b;">Agency</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Awards</th>
                      <th style="padding:8px;text-align:right;color:#64748b;">Total value</th>
                    </tr></thead>
                    <tbody>{agency_rows}</tbody>
                  </table>
                  {extra_sections_html}
                  {bid_table_html}

                  <div style="margin:28px 0 0;">
                  {email_button("https://www.booppa.io/tender-intelligence", "Open the dashboard →")}
                  {_pdf_btn}
                  </div>

                  <p style="margin:8px 0 0;font-size:12px;color:#94a3b8;">
                    You're receiving this as a Tender Intelligence subscriber.
                    <a href="https://www.booppa.io/account/billing" style="color:#7c3aed;">Manage subscription</a>
                  </p>
                    """,
                    title=f"Tender Intelligence — {period}",
                    preheader=f"Monthly GeBIZ sector trends for {period_label}.",
                )
                _asyncio.run(email_svc.send_html_email(sub.email, subject, body_html, category="marketing"))
                sent += 1
            except Exception as exc:
                logger.warning(f"[TenderIntelDigest] Failed for {sub.email}: {exc}")
                failed += 1

    except Exception as exc:
        logger.error(f"[TenderIntelDigest] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[TenderIntelDigest] Sent={sent} Failed={failed}")


@celery_app.task(name="fulfill_pdpa_declaration_task")
def fulfill_pdpa_declaration_task(user_id: str, customer_email: str | None = None):
    """Render + anchor + deliver a buyer's PDPA Level-2 self-declaration.

    Reads the user's submitted PdpaSelfDeclaration rows, generates a PDF,
    SHA-256 hashes it, uploads to S3, anchors the hash, creates a Report with
    framework="pdpa_self_declaration", and emails the PDF. Idempotent: skips if
    a pdpa_self_declaration Report with a tx_hash already exists.
    """
    from app.core.models import User, Report
    from app.core.models import PdpaSelfDeclaration
    from app.services.pdpa_declaration_generator import generate_pdpa_declaration_pdf

    db = SessionLocal()
    try:
        user = UserRepository.get_by_id(db, str(user_id))
        if not user:
            logger.warning("[PDPADeclaration] no user for id=%s", user_id)
            return
        email = customer_email or user.email

        existing = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "pdpa_self_declaration",
                Report.tx_hash.isnot(None),
            )
            .first()
        )
        if existing:
            logger.info("[PDPADeclaration] already fulfilled for %s", email)
            return

        rows = (
            db.query(PdpaSelfDeclaration)
            .filter(
                PdpaSelfDeclaration.user_id == user.id,
                PdpaSelfDeclaration.status == "submitted",
            )
            .all()
        )
        if not rows:
            logger.info("[PDPADeclaration] no submitted rows for %s", email)
            return

        keys = ["processing_purpose", "lawful_basis", "data_categories", "data_subjects",
                "recipients", "retention_period", "safeguards"]
        dicts = [{k: getattr(r, k) for k in keys} for r in rows]
        company_name = (getattr(user, "company", "") or "").strip() or "Your Organisation"
        uen = getattr(user, "uen", None) or "Not provided"

        pdf_bytes = generate_pdpa_declaration_pdf(
            company_name=company_name, uen=uen, rows=dicts,
        )
        file_hash = hashlib.sha256(pdf_bytes).hexdigest()

        s3 = S3Service()
        report_id = f"pdpa-self-declaration-{user.id}"
        s3_url = asyncio.run(s3.upload_pdf(pdf_bytes, report_id))

        tx_hash = None
        try:
            tx_hash = asyncio.run(
                BlockchainService().anchor_evidence(file_hash, metadata=f"pdpa_self_declaration:{report_id}")
            )
        except Exception as anchor_err:
            logger.warning("[PDPADeclaration] anchor failed for %s: %s", email, anchor_err)

        report = Report(
            owner_id=user.id,
            framework="pdpa_self_declaration",
            company_name=company_name,
            status="completed",
            tx_hash=tx_hash,
            audit_hash=file_hash,
            completed_at=datetime.now(timezone.utc),
            assessment_data={
                "file_hash": file_hash,
                "s3_key": f"reports/{report_id}.pdf",
                "s3_url": s3_url,
                "row_count": len(dicts),
                "original_filename": f"PDPA_Level2_Declaration_{user.id}.pdf",
                "blockchain_anchored_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        report.s3_url = s3_url
        report.file_key = f"reports/{report_id}.pdf"
        db.add(report)
        db.commit()
        logger.info("[PDPADeclaration] Generated + anchored for %s (rows=%d)", email, len(dicts))

        if email:
            from app.services.email_layout import branded_email_html
            body_html = branded_email_html(
                f"""
                <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">PDPA Level-2 Self-Declaration Ready</h2>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company_name}</strong>,</p>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your PDPA Level-2 self-declaration ({len(dicts)} processing activities) is attached
                   as a tamper-evident, blockchain-anchored PDF — it complements your PDPA Snapshot
                   (Level 1) to demonstrate PDPC Level 2 accountability.</p>
                <p style="color:#64748b;font-size:12px;margin:0;">Booppa · PDPA Level 2 · booppa.io</p>
                """,
                title="PDPA Level-2 Self-Declaration ready",
                preheader="Your blockchain-anchored PDPA Level-2 declaration is attached.",
            )
            try:
                ok = asyncio.run(EmailService().send_with_pdf_attachment(
                    to_email=email,
                    subject="Your PDPA Level-2 Self-Declaration",
                    body_html=body_html,
                    pdf_bytes=pdf_bytes,
                    filename=f"PDPA_Level2_Declaration_{company_name}.pdf".replace(" ", "_"),
                ))
                if not ok:
                    logger.error("[PDPADeclaration] delivery email rejected for %s", email)
            except Exception as mail_err:
                logger.error("[PDPADeclaration] email failed for %s: %s", email, mail_err)
    except Exception as exc:
        logger.error("[PDPADeclaration] Fulfillment error for %s: %s", user_id, exc)
        db.rollback()
    finally:
        db.close()


@celery_app.task(name="send_vendor_pro_monthly_competitor_signals")
def send_vendor_pro_monthly_competitor_signals():
    """
    Monthly competitor awareness signals for Vendor Pro subscribers.
    Generates a 1-page PDF with 3 GeBIZ-sourced signals and emails it as attachment.

    Anniversary-day cron — fires daily, processes subscribers whose anniversary
    matches today, keeping cadence consistent with other monthly deliverables.
    """
    from app.core.models import User, Subscription as SubModel
    from app.services.competitor_signals_generator import generate_and_deliver_competitor_signals

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        active_subs = (
            db.query(SubModel)
            .filter(
                SubModel.product_type.in_([
                    "vendor_pro_monthly", "vendor_pro_annual", "vendor_pro",
                ]),
                SubModel.status.in_(("active", "trialing")),
            )
            .all()
        )
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = (
            db.query(User)
            .filter(
                User.id.in_(user_ids),
                _anniversary_match_filter(User.subscription_anniversary_day),
            )
            .all()
            if user_ids else []
        )
        for user in subscribers:
            if not user.email:
                continue
            company = (getattr(user, "company", "") or "").strip() or user.email
            try:
                ok = asyncio.run(generate_and_deliver_competitor_signals(
                    vendor_id=str(user.id),
                    vendor_email=user.email,
                    company_name=company,
                    db=db,
                ))
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("[CompetitorSignals] Failed for %s: %s", user.email, exc)
                failed += 1
    finally:
        db.close()
    logger.info("[CompetitorSignals] Monthly digest: sent=%d failed=%d", sent, failed)


@celery_app.task(name="send_vendor_pro_daily_alerts")
def send_vendor_pro_daily_alerts():
    """
    Daily competitor-activity digest for Vendor Pro subscribers.

    For each active Vendor Pro user, find the tenders they've checked in the
    last 14d, then for each tender count new TenderCheckLookups in the last
    24h. If any tender has new activity, send the user an email summary.

    Runs at 00:00 UTC (08:00 SGT) via Celery Beat.
    """
    from app.core.models import User, Subscription as SubModel
    from app.core.models import TenderCheckLookup
    from app.billing.enforcement import VENDOR_PRO_PLAN_KEYS  # noqa: F401
    from sqlalchemy import func
    from datetime import timedelta
    import asyncio as _asyncio

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        now = datetime.now(timezone.utc)
        since_24h = (now - timedelta(hours=24)).replace(tzinfo=None)
        since_tracked = (now - timedelta(days=14)).replace(tzinfo=None)

        # All users with an active Vendor-Pro-tier (or superset) subscription.
        active_subs = (
            db.query(SubModel)
            .filter(
                SubModel.status.in_(("active", "trialing")),
                SubModel.product_type.in_([
                    "vendor_pro_monthly", "vendor_pro_annual", "vendor_pro",
                ]),
            )
            .all()
        )
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []

        if not subscribers:
            logger.info("[VendorProDaily] No active subscribers — skipping")
            return

        email_svc = EmailService()
        for sub in subscribers:
            try:
                # Tenders this user looked at in the last 14d
                tracked = (
                    db.query(TenderCheckLookup.tender_no)
                    .filter(
                        TenderCheckLookup.vendor_id == sub.id,
                        TenderCheckLookup.created_at >= since_tracked,
                        TenderCheckLookup.tender_no != None,  # noqa: E711
                    )
                    .distinct()
                    .all()
                )
                tender_nos = [t[0] for t in tracked if t[0]]
                if not tender_nos:
                    continue

                # Per-tender competitor activity in the last 24h (excluding this user)
                rows = (
                    db.query(
                        TenderCheckLookup.tender_no.label("tno"),
                        func.count(TenderCheckLookup.id).label("total"),
                        func.sum(
                            func.cast(TenderCheckLookup.is_verified, sa_int())
                        ).label("verified"),
                    )
                    .filter(
                        TenderCheckLookup.tender_no.in_(tender_nos),
                        TenderCheckLookup.created_at >= since_24h,
                        # Exclude the recipient's own lookups
                        (TenderCheckLookup.vendor_id != sub.id) | (TenderCheckLookup.vendor_id.is_(None)),
                    )
                    .group_by(TenderCheckLookup.tender_no)
                    .all()
                )
                if not rows:
                    continue

                row_html = ""
                for r in rows:
                    total = int(r.total or 0)
                    verified = int(r.verified or 0)
                    if total == 0:
                        continue
                    badge_color = "#ef4444" if verified >= 3 else "#d97706" if verified >= 1 else "#64748b"
                    row_html += f"""
                    <tr>
                      <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#0f172a;font-weight:500;">{r.tno}</td>
                      <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;color:#2563eb;text-align:right;">{total}</td>
                      <td style="padding:10px 8px;border-bottom:1px solid #e2e8f0;text-align:right;">
                        <span style="color:{badge_color};font-weight:600;">{verified}</span>
                      </td>
                    </tr>"""

                if not row_html:
                    continue

                subject = "Competitor activity on tenders you're tracking"
                from app.services.email_layout import branded_email_html, email_button
                body_html = branded_email_html(
                    f"""
                  <h2 style="margin:0 0 12px;font-size:20px;color:#0f172a;">Competitor activity — last 24 hours</h2>
                  <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">
                    Hi {sub.full_name or sub.company or sub.email},<br>
                    Other vendors ran probability checks on tenders you're tracking. Counts only — no identities shown.
                  </p>
                  <table style="width:100%;border-collapse:collapse;margin:20px 0;font-size:0.9em;">
                    <thead>
                      <tr style="border-bottom:1px solid #e2e8f0;">
                        <th style="padding:8px;text-align:left;color:#64748b;font-weight:600;">Tender</th>
                        <th style="padding:8px;text-align:right;color:#64748b;font-weight:600;">Lookups</th>
                        <th style="padding:8px;text-align:right;color:#64748b;font-weight:600;">Verified</th>
                      </tr>
                    </thead>
                    <tbody>{row_html}</tbody>
                  </table>
                  {email_button("https://www.booppa.io/vendor/dashboard", "Open Vendor Pro dashboard →")}
                  <p style="margin:8px 0 0;font-size:12px;color:#94a3b8;">
                    You can opt out of being counted in these signals from your dashboard.
                  </p>
                    """,
                    title="Competitor activity — last 24 hours",
                    preheader="Other vendors checked tenders you're tracking.",
                )
                _asyncio.run(email_svc.send_html_email(sub.email, subject, body_html, category="marketing"))
                sent += 1
            except Exception as exc:
                logger.warning(f"[VendorProDaily] Failed for {sub.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[VendorProDaily] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[VendorProDaily] Sent={sent} Failed={failed}")


def sa_int():
    """Lazy import of sqlalchemy.Integer so `func.cast` works above."""
    from sqlalchemy import Integer
    return Integer


@celery_app.task(name="weekly_intelligence_brief")
def weekly_intelligence_brief():
    """
    Send every vendor with a completed report their weekly intelligence brief.
    Runs Monday 00:00 UTC (08:00 SGT) via Celery Beat.
    Distinct from send_weekly_vendor_scores — this targets all vendors with any
    completed report, not just those with a VendorScore record.
    """
    from app.core.models import Report, User
    from app.core.models import VendorScore
    import asyncio as _asyncio

    db = SessionLocal()
    email_svc = EmailService()
    sent = 0
    failed = 0
    try:
        owner_ids = [
            str(r[0])
            for r in db.query(Report.owner_id)
            .filter(Report.status == "completed")
            .distinct()
            .all()
        ]
        for owner_id in owner_ids:
            try:
                user = UserRepository.get_by_id(db, str(owner_id))
                if not user or not user.email:
                    continue
                score_row = db.query(VendorScore).filter(
                    VendorScore.vendor_id == owner_id
                ).first()
                score = score_row.total_score if score_row else 0
                if score >= 80:
                    position = "top 10% of your sector"
                elif score >= 60:
                    position = "above median"
                elif score >= 40:
                    position = "below median"
                else:
                    position = "bottom 25% — take action now"

                from app.services.email_layout import branded_email_html, email_button
                _tip = ("Your score is strong. Consider adding notarized documents to reach DEEP verification."
                        if score >= 60
                        else "Add PDPA Snapshot or Notarization to improve your score and move above median.")
                body_html = branded_email_html(
                    f"""
                    <h2 style="margin:0 0 16px;font-size:20px;color:#0f172a;">Weekly intelligence brief</h2>
                    <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Trust Score: <strong>{score}/100</strong> — {position}.</p>
                    <p style="margin:0 0 20px;color:#475569;font-size:14px;line-height:1.6;">{_tip}</p>
                    {email_button("https://www.booppa.io/vendor/dashboard", "View dashboard", primary=False)}
                    <p style="margin:16px 0 0;color:#94a3b8;font-size:11px;">You're receiving this because you have an active BOOPPA vendor profile.</p>
                    """,
                    title="Weekly intelligence brief",
                    preheader=f"Your Trust Score is {score}/100 — {position}.",
                )

                _asyncio.run(email_svc.send_html_email(
                    to_email=user.email,
                    subject="Your weekly BOOPPA profile brief",
                    body_html=body_html,
                    category="marketing",
                ))
                sent += 1
            except Exception as exc:
                logger.warning(f"[WeeklyBrief] Failed for {owner_id}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[WeeklyBrief] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[WeeklyBrief] Sent={sent} Failed={failed}")


@celery_app.task(name="recompute_all_vendor_percentiles")
def recompute_all_vendor_percentiles():
    """
    Recompute VendorStatusSnapshot (including sector percentile) for every VENDOR user.
    Runs Sunday at 23:00 UTC via Celery Beat — before the Monday score digest — so
    percentiles shown to government procurement officers are fresh.
    """
    from app.core.models import User
    from app.services.scoring import VendorScoreEngine

    db = SessionLocal()
    ok = 0
    failed = 0
    try:
        vendor_ids = [
            str(r[0])
            for r in db.query(User.id).filter(User.role == "VENDOR").all()
        ]
        for vendor_id in vendor_ids:
            try:
                VendorScoreEngine.update_vendor_score(db, vendor_id)
                ok += 1
            except Exception as exc:
                logger.warning("[RecomputePercentiles] Failed vendor=%s: %s", vendor_id, exc)
                failed += 1
    except Exception as exc:
        logger.error("[RecomputePercentiles] Task aborted: %s", exc)
    finally:
        db.close()

    logger.info("[RecomputePercentiles] ok=%d failed=%d", ok, failed)


# ── Monthly intake refresh for compliance_evidence_monthly subscribers ──────
@celery_app.task(bind=True, max_retries=2, name="send_monthly_intake_refresh_task")
def send_monthly_intake_refresh_task(self):
    """Monthly: nudge compliance_evidence_monthly subscribers to confirm their
    intake before the next regen cycle. Without this, monthly Cover Sheets get
    anchored on-chain with stale intake facts (e.g. a DPO who's left, an ISO
    cert that's expired) and the per-answer 'verified' badges lose their
    defensibility.

    For each active subscriber we:
      1. Create a fresh PendingRfpIntake row (status='pending', tagged with
         bundle_source='compliance_evidence_monthly_refresh') so the existing
         /rfp-intake/{id} flow handles it identically to a new purchase.
      2. Pre-seed the rfp_intake:{session_id} cache with the buyer's last
         confirmed intake — the existing form pre-fill picks it up so most
         buyers just review + click Submit (30 seconds).
      3. Email a single-click link to /rfp-intake/{new_id}.

    Skip subscribers who already have a pending row younger than 14 days —
    avoids duplicate nudges if last month's email is still unanswered.
    """
    import uuid as _uuid
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.core.db import SessionLocal as _SL
    from app.core.models import User, Report
    from app.core.models import PendingRfpIntake
    from app.core.cache import cache as _cache
    from app.services.email_service import EmailService

    db = _SL()
    sent = 0
    skipped = 0
    failed = 0
    try:
        # Anniversary-day cron — fire ~6 days before each subscriber's cycle.
        # Use the short-month-aware filter with `now = today + 6 days` so a
        # Jan-31 subscriber gets the nudge correctly even when the +6-day
        # target lands in a short month.
        from app.workers.tasks import _anniversary_match_filter
        target_now = _dt.utcnow() + _td(days=6)
        subs = (
            db.query(User)
            .filter(
                User.subscription_tier == "compliance_evidence",
                _anniversary_match_filter(User.subscription_anniversary_day, now=target_now),
            )
            .all()
        )
        cutoff = _dt.utcnow() - _td(days=14)
        for user in subs:
            try:
                # Skip if there's already an outstanding pending intake — last
                # month's email is presumably still unread.
                outstanding = (
                    db.query(PendingRfpIntake)
                    .filter(
                        PendingRfpIntake.user_id == user.id,
                        PendingRfpIntake.status == "pending",
                        PendingRfpIntake.created_at >= cutoff,
                    )
                    .first()
                )
                if outstanding:
                    skipped += 1
                    continue

                # Pull last submitted intake's persisted data from the most
                # recent rfp_complete Report row. If none, we still create the
                # intake row — the form will start blank.
                last_report = (
                    db.query(Report)
                    .filter(Report.owner_id == user.id, Report.framework == "rfp_complete")
                    .order_by(Report.created_at.desc())
                    .first()
                )
                last_intake_data: dict = {}
                last_rfp_description: str = ""
                if last_report and isinstance(last_report.assessment_data, dict):
                    last_intake_data = last_report.assessment_data.get("intake_data") or {}
                    last_rfp_description = last_report.assessment_data.get("intake_rfp_description") or ""

                # Create a fresh PendingRfpIntake. Synthetic session_id since
                # this isn't tied to a Stripe checkout. Prefix marks the origin
                # so audit queries can trace these back to the monthly nudge.
                session_id = f"refresh_{user.id}_{_dt.utcnow().strftime('%Y%m')}_{_uuid.uuid4().hex[:8]}"
                row = PendingRfpIntake(
                    user_id=user.id,
                    session_id=session_id,
                    rfp_product_type="rfp_complete",
                    bundle_source="compliance_evidence_monthly_refresh",
                    vendor_url=(getattr(user, "website", "") or "") or last_report.company_website if last_report else None,
                    company_name=(getattr(user, "company", "") or "") or (last_report.company_name if last_report else None),
                    status="pending",
                )
                db.add(row)
                db.commit()
                intake_id = str(row.id)

                # Pre-seed the rfp_intake cache so the /rfp-intake/{id} form
                # picks up last month's answers via its existing prefill path.
                # 30-day TTL so the link works even if the buyer reads the email
                # late. The buyer's edits become the new authoritative source
                # on submit; this cache is just the pre-fill hint.
                if last_intake_data or last_rfp_description:
                    try:
                        _cache.set(
                            _cache.cache_key(f"rfp_intake:{session_id}"),
                            {
                                "rfp_description": last_rfp_description,
                                "intake_data": last_intake_data,
                            },
                            ttl=60 * 60 * 24 * 30,  # 30 days
                        )
                    except Exception as cache_err:
                        logger.warning(
                            "[MonthlyIntakeRefresh] cache prefill failed for %s: %s",
                            user.email, cache_err,
                        )

                # Send the confirmation email. Single CTA — the form lives at
                # /rfp-intake/{id}, which the buyer already used at signup.
                intake_url = f"https://www.booppa.io/rfp-intake/{intake_id}"
                body_html = f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#0f172a;">
                  <h2 style="color:#0f172a;margin:0 0 8px;">Confirm your intake for this month's evidence pack</h2>
                  <p style="color:#475569;margin:0 0 16px;line-height:1.5;">
                    Your <strong>Compliance Evidence</strong> subscription regenerates your blockchain-anchored
                    Cover Sheet at the start of every month. Before we anchor this month's pack,
                    take 30 seconds to confirm your facts are still current.
                  </p>
                  <p style="color:#475569;margin:0 0 16px;line-height:1.5;">
                    We've pre-filled your last confirmed answers — review each one, edit anything that
                    changed (new DPO contact, updated ISO cert, refreshed BCP test date, etc.), and submit.
                    Anything left unchanged carries forward.
                  </p>
                  <div style="text-align:center;margin:24px 0;">
                    <a href="{intake_url}"
                       style="display:inline-block;background:#0ea5e9;color:#fff;padding:12px 24px;
                              border-radius:8px;text-decoration:none;font-weight:bold;">
                      Confirm my intake (30 sec)
                    </a>
                  </div>
                  <p style="color:#64748b;font-size:13px;line-height:1.5;margin:24px 0 0;">
                    Why this matters: each answer in your kit is labelled with its verification source
                    (Intake / Website / ACRA / SSL Labs / GeBIZ). Stale intake = stale "verified" badges.
                    Keeping this confirmation current is what makes the kit defensible if a procurement
                    evaluator asks how a specific claim was verified.
                  </p>
                  <p style="color:#94a3b8;font-size:12px;margin:32px 0 0;">
                    Subscription managed at booppa.io/vendor/subscription.
                  </p>
                </div>
                """
                ok = asyncio.run(EmailService().send_html_email(
                    to_email=user.email,
                    subject="Confirm your monthly Compliance Evidence intake",
                    body_html=body_html,
                ))
                if ok:
                    sent += 1
                    logger.info(
                        "[MonthlyIntakeRefresh] sent to %s intake_id=%s prefill=%s",
                        user.email, intake_id, bool(last_intake_data or last_rfp_description),
                    )
                else:
                    failed += 1
                    logger.error(
                        "[MonthlyIntakeRefresh] email provider rejected for %s intake_id=%s",
                        user.email, intake_id,
                    )
            except Exception as per_user_err:
                failed += 1
                db.rollback()
                logger.warning(
                    "[MonthlyIntakeRefresh] failed for %s: %s",
                    getattr(user, "email", "?"), per_user_err,
                )
    finally:
        db.close()
    logger.info(
        "[MonthlyIntakeRefresh] complete · sent=%d skipped=%d failed=%d", sent, skipped, failed
    )


# ── Per-user instant first-cycle delivery ────────────────────────────────────
# Subscribers shouldn't wait up to 30 days for their first deliverable. These
# wrappers fire the same logic each tier's monthly cron does, scoped to one
# user. Called from stripe_webhook at subscription activation; also reused by
# the anniversary-day cron filtering.

def _load_user(user_id: str):
    """Load a User row by id, returning None if not found. Shared helper for
    the per-user task wrappers below."""
    db = SessionLocal()
    try:
        from app.core.models import User as _U
        return db.query(_U).filter(_U.id == user_id).first(), db
    except Exception:
        db.close()
        return None, None


@celery_app.task(bind=True, max_retries=2, name="run_vendor_active_check_for_user")
def run_vendor_active_check_for_user(self, user_id: str, override_company: str | None = None):
    """Per-user wrapper: queue Vendor Active's monthly health check.

    `override_company` is test-harness-only (admin Test Identity), threaded into
    the snapshot PDF without touching the real profile.
    """
    user, db = _load_user(user_id)
    try:
        if not user or not user.email:
            logger.warning("[VendorActiveFirstCycle] no user/email for id=%s", user_id)
            return
        vendor_active_health_check_task.delay(str(user.id), user.email, override_company, is_first_cycle=True)
    finally:
        if db:
            db.close()


@celery_app.task(bind=True, max_retries=2, name="run_pdpa_monitor_cycle_for_user")
def run_pdpa_monitor_cycle_for_user(self, user_id: str, override_website: str | None = None, override_company: str | None = None):
    """Per-user wrapper: queue PDPA Monitor's monthly rescan.

    `override_website` / `override_company` are test-harness-only (admin Test
    Identity): the scan + Monitor report reflect them without mutating the real
    profile. Production leaves them None and uses the stored profile.
    """
    user, db = _load_user(user_id)
    try:
        if not user or not user.email:
            logger.warning("[PdpaMonitorFirstCycle] no user/email for id=%s", user_id)
            return
        website = (override_website or "").strip() or (getattr(user, "website", "") or "").strip()
        if not website:
            logger.warning(
                "[PdpaMonitorFirstCycle] %s has no website — skipping initial scan, will run on next cycle after profile update",
                user.email,
            )
            return
        pdpa_monitor_monthly_rescan_task.delay(str(user.id), user.email, website, override_company)
    finally:
        if db:
            db.close()


@celery_app.task(bind=True, max_retries=2, name="run_vendor_pro_activation_for_user")
def run_vendor_pro_activation_for_user(self, user_id: str, override_website: str | None = None, override_company: str | None = None):
    """Per-user wrapper: Vendor Pro inherits Vendor Active's monthly health
    check + an immediate first PDPA rescan (Vendor Pro's quarterly cycle
    normally fires Jan/Apr/Jul/Oct; on activation we kick the first one now).

    `override_website` / `override_company` are test-harness-only (admin Test
    Identity); production leaves them None and uses the stored profile.
    """
    user, db = _load_user(user_id)
    try:
        if not user or not user.email:
            logger.warning("[VendorProFirstCycle] no user/email for id=%s", user_id)
            return
        # Health check (same as Vendor Active) — consolidated welcome digest
        vendor_active_health_check_task.delay(str(user.id), user.email, override_company, is_first_cycle=True)
        # First PDPA rescan if a website is configured
        website = (override_website or "").strip() or (getattr(user, "website", "") or "").strip()
        if website:
            pdpa_monitor_monthly_rescan_task.delay(str(user.id), user.email, website, override_company)
        else:
            logger.warning(
                "[VendorProFirstCycle] %s has no website — PDPA scan skipped; will fire after profile update",
                user.email,
            )
    finally:
        if db:
            db.close()


@celery_app.task(bind=True, max_retries=2, name="run_suite_trm_baseline_for_user")
def run_suite_trm_baseline_for_user(self, user_id: str, override_company: str | None = None):
    """Generate the MAS TRM Baseline Assessment PDF for a new suite subscriber.

    Standard/Pro Suite activation seeds 13 TRM control domains; this turns that
    into a tangible, board-presentable artifact and emails the buyer a download
    link — closing the audit gap where suites delivered only an email claiming
    "13 domains initialised" with nothing to show.
    """
    from app.core.models import User
    from app.core.models import Organisation, TrmControl, MAS_TRM_DOMAINS
    from app.services.trm_baseline_generator import generate_trm_baseline_pdf
    from app.services.storage import S3Service
    from app.services.email_service import EmailService

    user, db = _load_user(user_id)
    try:
        if not user or not user.email:
            logger.warning("[TRMBaseline] no user/email for id=%s", user_id)
            return
        plan = (getattr(user, "plan", "") or "")
        plan_label = "Pro Suite" if plan == "pro_suite" else "Standard Suite"

        org = (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user.id)
            .order_by(Organisation.created_at.asc())
            .first()
        )
        controls = []
        if org:
            rows = (
                db.query(TrmControl)
                .filter(TrmControl.organisation_id == org.id)
                .all()
            )
            # Order by sector criticality (fintech/healthcare lead with their
            # material domains) so the doc reads as sector-specific to a MAS
            # supervisor, falling back to canonical order when sector is unset.
            from app.services.trm_sector_override import reorder_controls_by_sector
            rows = reorder_controls_by_sector(rows, getattr(org, "sector", None))
            controls = [
                {
                    "domain": r.domain,
                    "control_ref": r.control_ref,
                    "status": r.status,
                    "risk_rating": r.risk_rating,
                    "gap_analysis": r.gap_analysis,
                }
                for r in rows
            ]
        if not controls:
            # Controls not yet seeded (race) — fall back to the canonical domains
            # so the buyer still receives a complete baseline.
            from app.services.trm_sector_override import reorder_controls_by_sector
            _seed = [{"domain": d, "control_ref": f"TRM-{i}", "status": "not_started"}
                     for i, d in enumerate(MAS_TRM_DOMAINS, 1)]
            controls = reorder_controls_by_sector(
                _seed, getattr(org, "sector", None) if org else None
            )

        # Assessed entity is the CUSTOMER. Prefer the test-harness override (so the
        # admin test-checkout never stamps the real account's company, e.g. "Booppa",
        # onto a customer's MAS document), then the user's company. Never fall back to
        # the Booppa platform name — use a neutral placeholder if truly unknown.
        company_name = (
            (override_company or "").strip()
            or (getattr(user, "company", "") or "").strip()
            or "Your Organisation"
        )

        # Configuration & provisioning evidence — tangible proof of what the
        # subscription unlocked (audit: suites showed "zero evidence of active
        # configuration", especially Pro's SSO/white-label/multi-subsidiary).
        # "Active" = live entitlement; "Ready" = provisioned, awaiting one-time
        # buyer setup at the linked page (kept honest — we don't claim SSO is
        # configured when it needs the buyer's IdP details).
        from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS
        _notar = ENTERPRISE_NOTARIZATION_LIMITS.get(plan, 50)
        provisioning = [
            {"capability": "MAS TRM workspace (13 domains)", "status": "Active",
             "detail": "Initialised — work each domain at booppa.io/vendor/trm"},
            {"capability": f"{_notar} notarizations / month", "status": "Active",
             "detail": "Included this cycle — redeem at booppa.io/notarize"},
            {"capability": "RESTful API + webhooks", "status": "Ready",
             "detail": "Generate an API key at booppa.io/vendor/api-keys"},
        ]
        if plan == "pro_suite":
            provisioning += [
                {"capability": "SSO — SAML 2.0 / OIDC", "status": "Ready",
                 "detail": "Configure your IdP at booppa.io/vendor/sso"},
                {"capability": "White-label reports", "status": "Ready",
                 "detail": "Enable + add your brand at booppa.io/vendor/profile"},
                {"capability": "Multi-subsidiary management", "status": "Ready",
                 "detail": "Add subsidiaries at booppa.io/vendor/subsidiaries"},
            ]

        pdf_bytes = generate_trm_baseline_pdf({
            "company_name": company_name,
            "plan_label": plan_label,
            "controls": controls,
            "provisioning": provisioning,
        })

        report_id = f"trm-baseline-{user.id}"
        download_url = None
        try:
            download_url = asyncio.run(S3Service().upload_pdf(pdf_bytes, report_id))
        except Exception as up_err:
            logger.error("[TRMBaseline] S3 upload failed for %s: %s", user.email, up_err)

        if download_url:
            from app.services.email_layout import branded_email_html, email_button
            body_html = branded_email_html(
                f"""
                <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · {plan_label}</p>
                <h2 style="margin:0 0 12px;color:#0f172a;font-size:20px;">Your MAS TRM Baseline is ready</h2>
                <p style="color:#334155;line-height:1.6;margin:0 0 20px;font-size:15px;">
                  We've prepared a baseline assessment of all 13 MAS Technology Risk Management control
                  domains for <strong>{company_name}</strong>. Use it as your starting inventory — then
                  work each domain (with AI gap analysis) in your TRM workspace.
                </p>
                {email_button(download_url, "Download your TRM Baseline (PDF)")}
                <p style="color:#64748b;font-size:13px;margin:8px 0 0;line-height:1.6;">
                  Open your <a href="https://www.booppa.io/vendor/trm" style="color:#10b981;">TRM workspace</a> to begin.
                </p>
                """,
                title="Your MAS TRM Baseline is ready",
                preheader=f"Baseline assessment of all 13 MAS TRM domains for {company_name}.",
            )
            sent = asyncio.run(EmailService().send_html_email(
                to_email=user.email,
                subject=f"Your MAS TRM Baseline Assessment — {plan_label}",
                body_html=body_html,
            ))
            if not sent:
                logger.error("[TRMBaseline] delivery email rejected for %s", user.email)
            else:
                logger.info("[TRMBaseline] Delivered baseline to %s (%d domains)", user.email, len(controls))
    except Exception as exc:
        logger.error("[TRMBaseline] Failed for %s: %s", user_id, exc)
        raise self.retry(exc=exc, countdown=120)
    finally:
        if db:
            db.close()


@celery_app.task(bind=True, max_retries=2, name="run_trm_board_report_for_user")
def run_trm_board_report_for_user(self, user_id: str, override_company: str | None = None):
    """Generate + email the monthly MAS TRM board report for one Suite user.

    Standard Suite → Booppa co-brand; Pro Suite → white-label (the org's
    WhiteLabelConfig colours/header/footer + logo). Month-over-month delta comes
    from the prior 'trm_board_report' Report snapshot; a new snapshot is persisted
    each run. PDF is delivered as a direct email attachment.
    """
    from app.core.models import Report, User
    from app.core.models import (
        MAS_TRM_DOMAINS, Organisation, TrmControl, WhiteLabelConfig,
    )
    from app.services.trm_board_report_generator import (
        TRM_BOARD_REPORT_SCHEMA_VERSION, board_data_from_controls,
        generate_trm_board_report_pdf,
    )
    from app.services.storage import S3Service
    from app.services.email_layout import branded_email_html

    db = SessionLocal()
    try:
        user = UserRepository.get_by_id(db, str(user_id))
        if not user or not user.email:
            return
        plan = (user.plan or "").lower()
        is_pro = plan.startswith("pro")
        plan_label = "Pro Suite" if is_pro else "Standard Suite"
        company_name = (
            (override_company or "").strip()
            or (getattr(user, "company", "") or "").strip()
            or "Your Organisation"
        )
        org = (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user.id)
            .order_by(Organisation.created_at.asc())
            .first()
        )
        if org:
            controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
        else:
            controls = [{"domain": d, "status": "not_started"} for d in MAS_TRM_DOMAINS]
        board = board_data_from_controls(controls, getattr(org, "sector", None) if org else None)

        # Month-over-month delta from the most recent prior board snapshot.
        prev_report = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "trm_board_report",
                    Report.status == "completed")
            .order_by(Report.completed_at.desc().nullslast())
            .first()
        )
        prev_pct = None
        if prev_report and isinstance(prev_report.assessment_data, dict):
            _p = prev_report.assessment_data.get("compliant_pct")
            prev_pct = int(_p) if isinstance(_p, (int, float)) else None

        # Pro white-label config (colours/header/footer + optional logo bytes).
        white_label = None
        if is_pro and org:
            wl = db.query(WhiteLabelConfig).filter(WhiteLabelConfig.organisation_id == org.id).first()
            if wl:
                logo_bytes = None
                if wl.logo_s3_key:
                    try:
                        _s3 = S3Service()
                        logo_bytes = _s3.s3_client.get_object(
                            Bucket=_s3.bucket, Key=wl.logo_s3_key
                        )["Body"].read()
                    except Exception:
                        logo_bytes = None
                white_label = {
                    "primary_color": wl.secondary_color or "#0f172a",
                    "secondary_color": wl.primary_color or "#10b981",
                    "footer_text": wl.footer_text,
                    "report_header_text": wl.report_header_text or company_name,
                    "logo_bytes": logo_bytes,
                }

        pdf_bytes = generate_trm_board_report_pdf({
            "company_name": company_name,
            "plan_label": plan_label,
            "domains": board["domains"],
            "compliant_pct": board["compliant_pct"],
            "previous_pct": prev_pct,
            "top_risks": board["top_risks"],
            "next_focus": board["next_focus"],
            "white_label": white_label,
        })

        month_label = datetime.now(timezone.utc).strftime("%B %Y")
        report_url = None
        # Stable S3 key so the self-serve endpoint can re-presign (presigned URLs
        # expire after 7 days; the report is fetched on demand months later).
        board_report_id = f"trm-board-{user.id}-{datetime.now(timezone.utc):%Y%m}"
        board_file_key = f"reports/{board_report_id}.pdf"
        try:
            report_url = asyncio.run(S3Service().upload_pdf(pdf_bytes, board_report_id))
        except Exception as up_err:
            logger.error("[TRMBoard] S3 upload failed for %s: %s", user.email, up_err)

        _safe = (company_name or "report").replace("/", "-").replace(" ", "-")
        sent = asyncio.run(EmailService().send_html_email(
            to_email=user.email,
            subject=f"Your MAS TRM Board Report — {month_label} ({plan_label})",
            body_html=branded_email_html(
                f"""
                <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{company_name}</strong>,</p>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your monthly MAS TRM board report for {month_label} is
                <strong>attached as a PDF</strong> — overall compliance {board['compliant_pct']}%,
                RAG status per domain, top open risks, and next month's focus.</p>
                <p style="color:#64748b;font-size:12px;line-height:1.6;">Open your
                <a href="https://www.booppa.io/vendor/trm" style="color:#10b981;">TRM workspace</a> to update controls.</p>
                """,
                title=f"Your MAS TRM Board Report — {month_label}",
                preheader=f"Overall compliance {board['compliant_pct']}% — attached as PDF.",
            ),
            attachments=[(f"MAS-TRM-Board-Report-{_safe}-{month_label}.pdf", pdf_bytes)],
        ))
        if not sent:
            logger.error("[TRMBoard] delivery email rejected for %s", user.email)

        # Persist this month's snapshot for next month's delta.
        try:
            db.add(Report(
                owner_id=user.id,
                framework="trm_board_report",
                company_name=company_name,
                assessment_data={
                    "compliant_pct": board["compliant_pct"],
                    "schema_version": TRM_BOARD_REPORT_SCHEMA_VERSION,
                    "s3_url": report_url,
                    "s3_key": board_file_key,
                    "plan_label": plan_label,
                },
                status="completed",
                s3_url=report_url,
                file_key=board_file_key,
                completed_at=datetime.now(timezone.utc),
            ))
            db.commit()
        except Exception as persist_err:
            logger.warning("[TRMBoard] snapshot persist failed for %s: %s", user.email, persist_err)
            db.rollback()
    except Exception as exc:
        logger.error("[TRMBoard] Failed for %s: %s", user_id, exc)
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()


@celery_app.task(name="run_trm_monthly_board_reports")
def run_trm_monthly_board_reports():
    """Monthly fan-out: enqueue the TRM board report for every active Suite user."""
    db = SessionLocal()
    queued = 0
    try:
        from app.core.models import User, Subscription as SubModel
        subs = db.query(SubModel).filter(
            SubModel.product_type.in_([
                "standard_suite", "standard_suite_monthly", "standard_suite_annual",
                "pro_suite", "pro_suite_monthly", "pro_suite_annual",
            ]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in subs if s.user_id}
        for uid in user_ids:
            run_trm_board_report_for_user.delay(str(uid))
            queued += 1
    except Exception as exc:
        logger.error("[TRMBoard] monthly fan-out failed: %s", exc)
    finally:
        db.close()
    logger.info("[TRMBoard] queued %d monthly board report(s)", queued)


@celery_app.task(bind=True, max_retries=2, name="fulfill_evidence_pack_task")
def fulfill_evidence_pack_task(self, evidence_pack_id: str):
    """Generate + deliver the BCEP PDPA Compliance Evidence Pack (7 documents).

    Runs after the buyer submits the structured intake. Pipeline: generate 7 docs
    via DeepSeek → anchor each hash + a master hash on the existing chain (Amoy
    testnet) → build branded DRAFT PDFs → upload to S3 → email the buyer the pack
    with per-document download links and the client-verification one-pager. The
    EvidencePack row is updated through each stage so the result page can poll it.
    """
    import hashlib
    from datetime import datetime as _dt, timezone as _tz
    from app.core.models import User
    from app.core.models import EvidencePack
    from app.services.evidence_pack import generate_evidence_pack, build_single_pdf, DOC_META
    from app.services.storage import S3Service
    from app.services.blockchain import BlockchainService

    explorer = settings.active_polygon_explorer_url.rstrip("/")
    db = SessionLocal()
    try:
        row = db.query(EvidencePack).filter(EvidencePack.id == evidence_pack_id).first()
        if not row:
            logger.warning("[EvidencePack] row not found: %s", evidence_pack_id)
            return
        intake = row.intake if isinstance(row.intake, dict) else {}
        if not intake.get("org_name"):
            logger.warning("[EvidencePack] %s has no intake — cannot generate", evidence_pack_id)
            row.status = "error"; row.error = "Missing intake"; db.commit()
            return

        # 0. Gather observed evidence so the documents reflect real signals, not
        #    just the intake form. Best-effort — a scan failure never blocks the
        #    pack; generation proceeds with whatever evidence is available.
        scan_evidence: dict = {}
        try:
            from app.core.models import Report
            domain = (intake.get("domain") or "").strip()
            if not domain:
                buyer_for_domain = db.query(User).filter(User.id == row.user_id).first()
                domain = (getattr(buyer_for_domain, "website", "") or "").strip()
            if domain:
                from app.services.pdpa_free_scan_service import run_free_scan
                try:
                    scan_evidence["website_scan"] = run_free_scan(domain)
                except Exception as se:
                    logger.warning("[EvidencePack] website scan failed for %s: %s", domain, se)
            # Reuse the buyer's most recent completed PDPA scan report if present.
            prior_pdpa = (
                db.query(Report)
                .filter(
                    Report.owner_id == row.user_id,
                    Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                    Report.status == "completed",
                )
                .order_by(Report.created_at.desc())
                .first()
            )
            if prior_pdpa and isinstance(prior_pdpa.assessment_data, dict):
                pf = prior_pdpa.assessment_data.get("findings")
                if isinstance(pf, list) and pf:
                    scan_evidence["pdpa_report"] = {"findings": pf}
        except Exception as ee:
            logger.warning("[EvidencePack] evidence gathering error for %s: %s", evidence_pack_id, ee)
        if scan_evidence:
            row.scan_evidence = scan_evidence
            db.commit()

        # 1. Generate the 7 documents (grounded in scan_evidence when available).
        row.status = "generating"; db.commit()
        pack = generate_evidence_pack(intake, scan_evidence=scan_evidence or None)
        if pack.get("errors"):
            logger.warning("[EvidencePack] %s generation errors: %s", evidence_pack_id, pack["errors"])

        # 2. Anchor each document hash + the master hash (Amoy testnet, best-effort).
        row.status = "anchoring"; db.commit()
        anchoring: dict = {}

        async def _anchor_all():
            bsvc = BlockchainService()
            for dt, h in (pack.get("hashes") or {}).items():
                try:
                    tx = await bsvc.anchor_evidence(h, metadata=f"evidence_pack:{dt}:{pack['pack_id']}")
                    if tx:
                        anchoring[dt] = {
                            "tx_hash": tx,
                            "verification_url": f"{explorer}/tx/{tx}",
                            "anchor_time_utc": _dt.now(_tz.utc).isoformat(),
                        }
                except Exception as ae:
                    logger.warning("[EvidencePack] anchor failed for %s: %s", dt, ae)
            try:
                mtx = await bsvc.anchor_evidence(pack["master_hash"], metadata=f"evidence_pack:MASTER:{pack['pack_id']}")
                if mtx:
                    anchoring["master"] = {
                        "tx_hash": mtx,
                        "verification_url": f"{explorer}/tx/{mtx}",
                        "anchor_time_utc": _dt.now(_tz.utc).isoformat(),
                    }
            except Exception as me:
                logger.warning("[EvidencePack] master anchor failed: %s", me)

        try:
            asyncio.run(_anchor_all())
        except Exception as anchor_err:
            logger.warning("[EvidencePack] anchoring stage error: %s", anchor_err)
        pack["anchoring"] = anchoring

        # 3. Build PDFs + upload to S3.
        row.status = "building_pdfs"; db.commit()
        download_urls: dict = {}
        for dt in DOC_META:
            if dt not in pack.get("documents", {}):
                continue
            try:
                pdf_bytes = build_single_pdf(pack, dt, "")  # falsy path → returns bytes
                url = asyncio.run(S3Service().upload_pdf(pdf_bytes, f"evidence-pack-{pack['pack_id']}-{dt}"))
                if url:
                    download_urls[dt] = url
            except Exception as pe:
                logger.error("[EvidencePack] PDF/upload failed for %s: %s", dt, pe)

        # 4. Completeness gate — the pack is sold as SEVEN governance documents.
        # A partial pack (a doc failed to generate, or built but failed to
        # upload) must NOT be delivered as "ready": the forensic finding was a
        # 5/7 pack shipped to a paying customer with the cover sheet still
        # listing the two missing docs. Require every DOC_META doc_type to be
        # both generated AND present in the delivered download set.
        _required = set(DOC_META.keys())
        _generated = set((pack.get("documents") or {}).keys())
        _delivered = set(download_urls.keys())
        _missing_gen = _required - _generated
        _missing_del = _required - _delivered
        if _missing_gen or _missing_del:
            missing = sorted(_missing_gen | _missing_del)
            logger.error(
                "[EvidencePack] %s incomplete — generated %d/%d, delivered %d/%d; "
                "missing=%s; errors=%s",
                evidence_pack_id, len(_generated), len(_required),
                len(_delivered), len(_required), missing, pack.get("errors"),
            )
            row.documents = pack.get("documents")
            row.hashes = pack.get("hashes")
            row.master_hash = pack.get("master_hash")
            row.anchoring = anchoring
            row.download_urls = download_urls
            row.status = "error"
            row.error = f"incomplete pack — missing docs: {missing}"[:1000]
            db.commit()
            try:
                buyer_for_alert = db.query(User).filter(User.id == row.user_id).first()
                from app.services.fulfillment import alert_payment_fulfillment_issue
                asyncio.run(alert_payment_fulfillment_issue(
                    reason=f"Evidence Pack {pack.get('pack_id')} incomplete: missing {missing}",
                    product_type="compliance_evidence_pack",
                    customer_email=(buyer_for_alert.email if buyer_for_alert else None),
                    session_id=row.session_id,
                    notify_customer=False,
                ))
            except Exception as _ae:
                logger.warning("[EvidencePack] incomplete-pack alert failed: %s", _ae)
            # Retry the whole generation — a transient AI/upload failure should
            # self-heal rather than leave the buyer with a partial pack.
            raise RuntimeError(f"incomplete evidence pack: missing {missing}")

        # Persist the finished (complete) pack.
        row.documents = pack.get("documents")
        row.hashes = pack.get("hashes")
        row.master_hash = pack.get("master_hash")
        row.anchoring = anchoring
        row.download_urls = download_urls
        row.status = "ready"
        db.commit()

        # 4a. The cover sheet is the centerpiece of the Compliance Evidence Pack
        # and indexes this 7-doc pack. It waits on PDPA + RFP + this pack; now
        # that the pack is ready, re-check inline so the sheet fires immediately
        # instead of waiting up to an hour for the `sweep_pending_cover_sheets`
        # backstop. Best-effort — must never fail the pack delivery.
        try:
            from app.services.fulfillment import maybe_fire_cover_sheet

            buyer_for_cs = db.query(User).filter(User.id == row.user_id).first()
            if buyer_for_cs and buyer_for_cs.email:
                maybe_fire_cover_sheet(buyer_for_cs.email)
        except Exception as cs_err:
            logger.warning("[EvidencePack] cover-sheet re-fire failed (non-blocking): %s", cs_err)

        # 4b. Cache by session so the result page can fetch it (mirrors RFP).
        if row.session_id and download_urls:
            try:
                from app.core.cache import cache as cache_mod
                cache_mod.set(
                    cache_mod.cache_key(f"evidence_pack_result:{row.session_id}"),
                    {
                        "pack_id": pack["pack_id"],
                        "organisation": pack["organisation"],
                        "download_urls": download_urls,
                        "master_hash": pack.get("master_hash"),
                        "master_tx": (anchoring.get("master") or {}).get("tx_hash"),
                    },
                    ttl=604800,
                )
            except Exception as ce:
                logger.warning("[EvidencePack] result cache failed: %s", ce)

        # 5. Email the buyer the pack.
        buyer = db.query(User).filter(User.id == row.user_id).first()
        to_email = buyer.email if buyer else None
        if to_email and download_urls:
            links = "".join(
                f'<li style="margin:6px 0;"><a href="{u}" style="color:#10b981;">'
                f'{DOC_META.get(dt, {}).get("title", dt)}</a></li>'
                for dt, u in download_urls.items()
            )
            network = settings.active_polygon_network_name
            from app.services.email_layout import branded_email_html
            body_html = branded_email_html(
                f"""
                <h2 style="color:#0f172a;margin:0 0 16px;font-size:20px;">Your PDPA Compliance Evidence Pack is ready</h2>
                <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello <strong>{pack['organisation']}</strong>,</p>
                <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your Evidence Pack of seven PDPA governance documents has been generated and
                   anchored. Each document is an <strong>AI-generated DRAFT</strong> — your
                   authorised representative must review, correct, and sign it before it carries
                   evidentiary value.</p>
                <ul style="font-size:14px;padding-left:20px;color:#334155;line-height:1.8;">{links}</ul>
                <p style="color:#334155;font-size:14px;line-height:1.6;">Your Compliance Evidence Pack also includes a
                   <strong>PDPA Snapshot scan</strong> and an <strong>RFP Complete kit</strong>. These
                   arrive in their own emails as each finishes generating.</p>
                <p style="color:#64748b;font-size:12px;line-height:1.6;">Anchored on the {network} for
                   tamper-checking. A testnet timestamp evidences existence; it is not a mainnet or
                   RFC 3161 timestamp. Not legal advice; does not certify PDPA compliance.</p>
                """,
                title="Your PDPA Compliance Evidence Pack is ready",
                preheader="Seven PDPA governance documents, generated and anchored.",
            )
            sent = asyncio.run(EmailService().send_html_email(
                to_email=to_email,
                subject="Your PDPA Compliance Evidence Pack (7 documents)",
                body_html=body_html,
            ))
            if not sent:
                logger.error("[EvidencePack] delivery email rejected for %s", to_email)
        logger.info("[EvidencePack] %s ready — %d docs delivered", evidence_pack_id, len(download_urls))
    except Exception as exc:
        logger.error("[EvidencePack] Failed for %s: %s", evidence_pack_id, exc)
        try:
            row = db.query(EvidencePack).filter(EvidencePack.id == evidence_pack_id).first()
            if row:
                row.status = "error"; row.error = str(exc)[:1000]; db.commit()
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="run_compliance_evidence_cycle_for_user")
def run_compliance_evidence_cycle_for_user(self, user_id: str, test_simulation: bool = False, override_website: str | None = None, override_company: str | None = None):
    """Per-user wrapper: Compliance Evidence bundle regen for a single user.

    `test_simulation` (admin simulate-purchase) flows into the bundle metadata so
    the RFP component auto-generates instead of emailing a brief-intake link.
    `override_website` / `override_company` are test-harness-only (admin Test
    Identity); production leaves them None and uses the stored profile.
    """
    user, db = _load_user(user_id)
    try:
        if not user or not user.email:
            logger.warning("[CEFirstCycle] no user/email for id=%s", user_id)
            return
        website = (override_website or "").strip() or (getattr(user, "website", "") or "").strip()
        # CE now also produces the BCEP evidence pack, which is driven by a
        # structured intake (not a website scan), so a missing website no longer
        # blocks the cycle — the buyer supplies the domain in the intake form.
        # The cover-sheet flags (pending_cover_sheet / compliance_evidence_credits /
        # signed_cover_sheet_uploaded) are NOT set here on purpose: the delegated
        # `fulfill_bundle_task` credit-grant block sets them for every CE path
        # (see stripe_webhook.py:_fulfill_bundle). The cover sheet is the
        # centerpiece of the pack and fires once PDPA + RFP + the BCEP pack are all
        # ready (see `_maybe_fire_cover_sheet`).
        fulfill_bundle_task.delay(
            product_type="compliance_evidence_pack",
            session_id=None,
            customer_email=user.email,
            metadata={
                "company_name": (override_company or "").strip() or getattr(user, "company", ""),
                "vendor_url": website,
                "subscription_cycle": True,
                **({"test_simulation": "1"} if test_simulation else {}),
            },
            report_id=None,
        )
    finally:
        if db:
            db.close()


@celery_app.task(bind=True, max_retries=2, name="send_tender_intelligence_digest_for_user")
def send_tender_intelligence_digest_for_user(self, user_id: str):
    """Per-user wrapper: send the Tender Intelligence digest immediately to one
    subscriber. Delegates to the bulk task with `target_user_id` set so the
    same code path handles aggregation + email; only one row gets sent.
    """
    try:
        send_tender_intelligence_digest(target_user_id=user_id)
    except Exception as e:
        logger.warning(
            "[TenderIntelFirstCycle] failed for user=%s: %s", user_id, e,
        )


@celery_app.task(bind=True, max_retries=4, name="anchor_scan_ledger_task")
def anchor_scan_ledger_task(self, ledger_id: str):
    """Anchor a buyer scan on Polygon for the Buyer Enterprise on-chain
    verification log. Computes a SHA-256 over the ledger fields, anchors it,
    and stores tx_hash/anchored_at on the row. Non-blocking; tolerant of a
    not-yet-committed row (retries) and of missing blockchain config (no-op)."""
    from app.core.models import VendorScanLedger
    from datetime import datetime as _dt, timezone as _tz

    if not getattr(settings, "BLOCKCHAIN_PRIVATE_KEY", None):
        logger.info("[scan-anchor] BLOCKCHAIN_PRIVATE_KEY unset — skipping %s", ledger_id)
        return

    db = SessionLocal()
    try:
        row = db.query(VendorScanLedger).filter(VendorScanLedger.id == ledger_id).first()
        if not row:
            # Request commit may not have landed yet — retry a few times.
            raise self.retry(countdown=10, exc=RuntimeError(f"ledger {ledger_id} not found yet"))
        if row.tx_hash:
            return  # already anchored

        scan_data = {
            "buyer_id": str(row.buyer_id),
            "vendor_id": str(row.vendor_id),
            "scan_type": row.scan_type,
            "month": row.month,
            "plan_at_consumption": row.plan_at_consumption,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        scan_hash = hashlib.sha256(json.dumps(scan_data, sort_keys=True).encode()).hexdigest()

        tx_hash = asyncio.run(BlockchainService().anchor_evidence(
            scan_hash, metadata=f"scan_ledger:{row.scan_type}:{ledger_id}",
        ))
        if tx_hash:
            row.tx_hash = tx_hash
            row.anchored_at = _dt.now(_tz.utc)
            row.anchor_error = None
            db.commit()
            logger.info("[scan-anchor] anchored ledger %s -> %s", ledger_id, tx_hash[:16])
        else:
            logger.info("[scan-anchor] anchor_evidence returned None (idempotent) for %s", ledger_id)
    except self.MaxRetriesExceededError:
        logger.warning("[scan-anchor] gave up on %s (row never committed)", ledger_id)
    except Exception as exc:
        try:
            raise self.retry(countdown=60 * (2 ** self.request.retries), exc=exc)
        except self.MaxRetriesExceededError:
            db.rollback()
            try:
                row = db.query(VendorScanLedger).filter(VendorScanLedger.id == ledger_id).first()
                if row:
                    row.anchor_error = str(exc)[:500]
                    db.commit()
            except Exception:
                pass
            logger.error("[scan-anchor] failed for %s: %s", ledger_id, exc)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="bulk_pdpa_scan_item_task", rate_limit="20/m")
def bulk_pdpa_scan_item_task(self, item_id: str):
    """Run one PDPA free scan for an admin bulk-scan item.

    rate_limit="20/m" is the throttle that lets a 600-row batch drain in ~30
    minutes without starving paid work on the `reports` queue. The scan itself
    is run_free_scan() — HTTP-only, no AI/S3/blockchain — so the only shared
    resource each task touches is one short-lived DB session per phase.
    """
    from app.core.models import PdpaBulkScanItem
    from app.services.pdpa_free_scan_service import run_free_scan
    from datetime import datetime as _bulk_dt

    db = SessionLocal()
    try:
        item = db.query(PdpaBulkScanItem).filter(PdpaBulkScanItem.id == item_id).first()
        if not item or item.status in ("done", "failed"):
            return
        item.status = "running"
        db.commit()
        website_url = item.website_url
    finally:
        db.close()

    try:
        result = run_free_scan(website_url)
    except Exception as exc:
        try:
            raise self.retry(countdown=60 * (2 ** self.request.retries), exc=exc)
        except self.MaxRetriesExceededError:
            result = None
            error = str(exc)[:500]
    else:
        error = None

    db = SessionLocal()
    try:
        item = db.query(PdpaBulkScanItem).filter(PdpaBulkScanItem.id == item_id).first()
        if not item:
            return
        item.status = "done" if result is not None else "failed"
        item.result = result
        item.error = error
        item.finished_at = _bulk_dt.utcnow()
        db.commit()
    finally:
        db.close()
