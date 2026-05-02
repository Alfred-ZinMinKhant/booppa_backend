from .celery_app import celery_app
from app.core.db import SessionLocal
from app.core.models import Report
from app.services.ai_service import AIService
from app.services.booppa_ai_service import BooppaAIService
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.storage import S3Service
from app.services.email_service import EmailService
from app.services.screenshot_service import capture_screenshot_base64
from app.core.config import settings
from app.billing.enforcement import enforce_tier
from app.services.audit_chain import append_audit_event
from app.services.dependency_logger import log_dependency_event
from app.services.verify_registry import register_verification
from app.integrations.ai.adapter import ai_preview
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


async def _capture_screenshot_with_timeout(url: str, timeout: int = 25) -> str | None:
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
    """
    from urllib.parse import quote_plus
    _MIN = 8_000

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:

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
                        if img_resp.status_code == 200 and len(img_resp.content) > _MIN:
                            logger.info(f"Screenshot via Microlink for {url}")
                            return base64.b64encode(img_resp.content).decode(), None
        except Exception as e:
            logger.warning(f"Microlink failed for {url}: {e}")

        # 2. Thum.io
        try:
            resp = await client.get(f"https://image.thum.io/get/width/1400/{url}")
            if resp.status_code == 200 and len(resp.content) > _MIN:
                logger.info(f"Screenshot via Thum.io for {url}")
                return base64.b64encode(resp.content).decode(), None
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
                if resp.status_code == 200 and len(resp.content) > _MIN:
                    logger.info(f"Screenshot via mshots (attempt {attempt+1}) for {url}")
                    return base64.b64encode(resp.content).decode(), None
                break
            except Exception as e:
                logger.warning(f"mshots attempt {attempt+1} error for {url}: {e}")
                break

        # 4. Screenshot.guru
        try:
            resp = await client.get(
                f"https://screenshot.guru/api?url={quote_plus(url)}&width=1400"
            )
            if resp.status_code == 200 and len(resp.content) > _MIN:
                logger.info(f"Screenshot via Screenshot.guru for {url}")
                return base64.b64encode(resp.content).decode(), None
            logger.warning(f"Screenshot.guru status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Screenshot.guru error for {url}: {e}")

    return None, "all_providers_failed"


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
            
            found = [k for k in indicators if k in html]
            if found or banner_visible:
                return {
                    "consent_mechanism": {
                        "has_cookie_banner": True,
                        "has_active_consent": True,
                        "detected_providers": found,
                        "rendered_detection": True,
                    }
                }
    except Exception as e:
        logger.warning(f"Playwright cookie detection failed, falling back to HTTP: {e}")

    # Fallback to static HTTP scan (browser-like headers to avoid 403)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
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


async def _scan_site_metadata(url: str | None) -> dict:
    if not url:
        return {}

    headers_result = {}
    page_result = {}
    html = ""
    site_accessible = False
    http_status = 0

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
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
    if privacy_link:
        try:
            privacy_url = (
                privacy_link
                if privacy_link.startswith("http")
                else urljoin(url, privacy_link)
            )
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(
                    privacy_url, headers=_BROWSER_UA_HEADERS
                )
                if resp.status_code < 400:
                    combined_html += "\n" + (resp.text or "").lower()
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

    # NRIC hints detection
    nric_word = re.search(r"\bnric\b", combined_html)
    fin_word = re.search(r"\bfin\b", combined_html)
    fin_context = "fin number" in combined_html or "fin no" in combined_html
    input_nric = re.search(r"name=\"[^\"]*(nric|fin)[^\"]*\"", combined_html)
    collects_nric = bool(nric_word or (fin_word and fin_context) or input_nric)
    page_result["collects_nric"] = bool(collects_nric)
    if collects_nric:
        page_result["nric_evidence"] = "NRIC/FIN keyword detected in page content"

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

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
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
                final_url = str(resp.url)
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
            report = db.query(Report).filter(Report.id == report_id).first()
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
        report = db.query(Report).filter(Report.id == report_id).first()
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
            if isinstance(report.assessment_data, dict):
                resolved_url = report.assessment_data.get("resolved_url") or report.assessment_data.get("url")
            metadata_result = await _scan_site_metadata(resolved_url)
            if metadata_result:
                _set_assessment_values(report, metadata_result)
                db.commit()
        except Exception as e:
            logger.warning(f"Metadata scan failed for {report_id}: {e}")

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
            ):
                if _scan_key in report.assessment_data:
                    pdf_data[_scan_key] = report.assessment_data[_scan_key]

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
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": True,
                        "pdf_generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
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
            report = db.query(Report).filter(Report.id == report_id).first()
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
        from app.api.stripe_webhook import _activate_subscription
        asyncio.run(_activate_subscription(
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
        from app.api.stripe_webhook import _fulfill_bundle
        asyncio.run(_fulfill_bundle(
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
        from app.api.stripe_webhook import _fire_strategy_6
        asyncio.run(_fire_strategy_6(sector=sector, buyer_rfp_title=rfp_title))
        logger.info(f"[fire_strategy_6_task] sector={sector}")
    except Exception as exc:
        logger.warning(f"[fire_strategy_6_task] failed (attempt {self.request.retries + 1}): {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=2, name="send_referral_reward_email_task")
def send_referral_reward_email_task(self, referrer_email: str):
    """Celery task: send referral conversion reward notification email."""
    body_html = (
        "<html><body style='font-family:Arial;max-width:600px;'>"
        "<h2 style='color:#0f172a;'>Your referral paid off!</h2>"
        "<p>A vendor you referred just made their first purchase. "
        "30 free days have been added to your account.</p>"
        "<a href='https://www.booppa.io/vendor/dashboard' "
        "style='background:#10b981;color:#fff;padding:10px 20px;"
        "text-decoration:none;border-radius:6px;font-weight:bold;'>"
        "View dashboard</a>"
        "</body></html>"
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
        from app.api.stripe_webhook import _fulfill_vendor_proof
        asyncio.run(_fulfill_vendor_proof(report_id=report_id, customer_email=customer_email))
        logger.info(f"Vendor proof fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"Vendor proof fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_pdpa_task")
def fulfill_pdpa_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: generate PDPA PDF, update compliance score, write CertificateLog, send email."""
    try:
        from app.api.stripe_webhook import _fulfill_pdpa
        asyncio.run(_fulfill_pdpa(report_id=report_id, customer_email=customer_email))
        logger.info(f"PDPA snapshot fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"PDPA fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_notarization_task")
def fulfill_notarization_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: anchor, generate PDF, and deliver notarization certificate."""
    try:
        from app.api.stripe_webhook import _fulfill_notarization
        asyncio.run(_fulfill_notarization(report_id=report_id, customer_email=customer_email))
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
):
    """Celery task: generate and deliver the RFP Kit evidence package."""
    try:
        from app.api.stripe_webhook import _fulfill_rfp_package
        asyncio.run(_fulfill_rfp_package(
            product_type=product_type,
            vendor_id=vendor_id,
            vendor_email=vendor_email,
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_description=rfp_description,
            session_id=session_id,
            intake_data=intake_data,
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

        report_id = metadata.get("report_id") or str(_uuid.uuid4())

        # 1. Look up anchored bundle notarizations + PDPA + Vendor Proof for this user
        anchored_documents: list[dict] = []
        pdpa_score = metadata.get("pdpa_score", "—")
        pdpa_status = "Pending"
        vp_status = "Pending"
        explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")
        db = SessionLocal()
        try:
            user = (
                db.query(User).filter(User.email == customer_email).first()
                if customer_email else None
            )
            if user:
                # Pull bundle-redeemed notarizations
                rows = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework == "compliance_notarization",
                    )
                    .order_by(Report.created_at.desc())
                    .limit(20)
                    .all()
                )
                for r in rows:
                    ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
                    if not ad.get("bundle_credit_redeemed"):
                        continue
                    anchored_documents.append({
                        "filename": ad.get("original_filename") or "—",
                        "descriptor": ad.get("document_descriptor") or "",
                        "file_hash": ad.get("file_hash") or r.audit_hash or "—",
                        "tx_hash": r.tx_hash,
                        "tx_url": f"{explorer_base}/tx/{r.tx_hash}" if r.tx_hash else None,
                        "anchored_at": ad.get("blockchain_anchored_at"),
                    })
                # PDPA + VP status from latest matching reports
                pdpa_report = (
                    db.query(Report)
                    .filter(
                        Report.owner_id == user.id,
                        Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                    )
                    .order_by(Report.created_at.desc())
                    .first()
                )
                if pdpa_report:
                    pdpa_status = pdpa_report.status.title() if pdpa_report.status else "Pending"
                    pdpa_ad = pdpa_report.assessment_data if isinstance(pdpa_report.assessment_data, dict) else {}
                    if pdpa_ad.get("risk_score") is not None:
                        pdpa_score = pdpa_ad.get("risk_score")
                vp_report = (
                    db.query(Report)
                    .filter(Report.owner_id == user.id, Report.framework == "vendor_proof")
                    .order_by(Report.created_at.desc())
                    .first()
                )
                if vp_report:
                    vp_status = vp_report.status.title() if vp_report.status else "Pending"
        finally:
            db.close()

        # 2. Build cover data
        cover_data = {
            "report_id": report_id,
            "bundle_type": bundle_type,
            "company_name": company_name or metadata.get("company_name", ""),
            "customer_email": customer_email,
            "pdpa_status": pdpa_status,
            "pdpa_score": pdpa_score,
            "vendor_proof_status": vp_status,
            "notarization_count": len(anchored_documents),
            "anchored_documents": anchored_documents,
            "tx_hash": "—",
            "network": settings.POLYGON_NETWORK_NAME,
            "recommendations": None,
            "trm_domains": [],
        }

        # 3. Anchor the cover sheet itself (digest of the included evidence)
        try:
            digest_input = "|".join(
                [d.get("file_hash", "") for d in anchored_documents] + [report_id]
            )
            content_hash = hashlib.sha256(digest_input.encode()).hexdigest()
            blockchain = BlockchainService()
            tx = asyncio.run(blockchain.anchor_evidence(content_hash, metadata=f"cover_sheet:{report_id}"))
            if tx:
                cover_data["tx_hash"] = tx
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

        # 6. Email delivery (link only — EmailService doesn't support attachments)
        if customer_email:
            email_svc = EmailService()
            doc_count = len(anchored_documents)
            doc_summary = (
                f"{doc_count} compliance document{'s' if doc_count != 1 else ''} anchored on {settings.POLYGON_NETWORK_NAME}"
                if doc_count
                else "Cover sheet generated — you can still upload remaining documents at booppa.io/compliance-evidence-pack/upload"
            )
            asyncio.run(email_svc.send_html_email(
                to_email=customer_email,
                subject="Your Compliance Evidence Pack is ready",
                body_html=(
                    f"<div style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;'>"
                    f"<h2 style='color:#0f172a;'>Compliance Evidence Pack</h2>"
                    f"<p style='color:#334155;'>Your bundle is complete. {doc_summary}.</p>"
                    f"<p><a href='{download_url}' style='background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;'>Download Cover Sheet PDF</a></p>"
                    f"<p style='color:#64748b;font-size:13px;'>This 9-section regulator-ready PDF includes your Vendor Proof status, PDPA scan results, and SHA-256 anchored evidence for each uploaded document.</p>"
                    f"</div>"
                ),
            ))
            logger.info(f"Cover sheet delivered to {customer_email} for {bundle_type} (docs={doc_count})")

    except Exception as exc:
        logger.error(f"Cover sheet fulfillment failed: {exc}")
        countdown = 120 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=2, name="vendor_active_health_check_task")
def vendor_active_health_check_task(self, vendor_id: str, vendor_email: str):
    """
    Monthly health check for Vendor Active subscribers.
    1. Recalculate vendor score
    2. Send monthly metrics email (profile views, search appearances, movement vs prior month)
    3. Competitor alert: notify if any sector peer improved verificationDepth this month
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
        user = db.query(User).filter(User.id == vendor_id).first()
        company = getattr(user, "company", "Your company") if user else "Your company"

        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        from app.core.models import VerifyRecord, ProofView
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
        profile_views = 0
        if verify:
            profile_views = db.query(ProofView).filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= thirty_days_ago,
            ).count()

        # 3. Email monthly digest
        email_svc = EmailService()
        asyncio.run(email_svc.send_html_email(
            to_email=vendor_email,
            subject=f"Your Monthly BOOPPA Health Check — {company}",
            body_html=f"""
            <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
              <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                <h1 style="color:#10b981;margin:0;font-size:20px;">Monthly Profile Health Check</h1>
              </div>
              <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                <p>Hello <strong>{company}</strong>,</p>
                <p>Here is your BOOPPA profile activity for the past 30 days:</p>
                <div style="background:#f8fafc;border-radius:8px;padding:20px;margin:20px 0;">
                  <p style="margin:4px 0;"><strong>Trust Score:</strong> {score_record.total_score}/100</p>
                  <p style="margin:4px 0;"><strong>Compliance Score:</strong> {score_record.compliance_score}/100</p>
                  <p style="margin:4px 0;"><strong>Profile Views (30d):</strong> {profile_views}</p>
                </div>
                <p>
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    View Full Dashboard →
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;margin-top:24px;">
                  Vendor Active — monthly health check · booppa.io
                </p>
              </div>
            </body></html>
            """,
        ))
        logger.info(f"Vendor Active health check completed for vendor {vendor_id}")
    except Exception as exc:
        logger.error(f"Vendor Active health check failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
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
    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
      <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
        <h1 style="color:#10b981;margin:0;font-size:20px;">PDPA Monitor — {month_label} Regulatory Alert</h1>
      </div>
      <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
        <p>Your monthly PDPA compliance briefing from BOOPPA.</p>
        <h3 style="color:#0f172a;">Key PDPC Updates This Month</h3>
        <ul>
          <li>Review your data breach notification procedures — PDPC enforcement actions increased 18% YoY.</li>
          <li>Ensure your Data Protection Officer (DPO) contact details are current on the PDPC register.</li>
          <li>Check that third-party data processors have signed updated data processing agreements.</li>
          <li>Verify your consent management records for any new marketing campaigns.</li>
        </ul>
        <h3 style="color:#0f172a;">Action Items</h3>
        <ul>
          <li>Log in to your BOOPPA dashboard to review your current compliance score.</li>
          <li>Upload any new compliance documents to maintain your verified status.</li>
        </ul>
        <p style="margin-top:24px;">
          <a href="https://www.booppa.io/vendor/dashboard"
             style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold;display:inline-block;">
            View Dashboard →
          </a>
        </p>
        <p style="color:#64748b;font-size:12px;margin-top:24px;">
          This alert is part of your PDPA Monitor subscription.<br>
          booppa.io · Singapore
        </p>
      </div>
    </body></html>
    """
    try:
        asyncio.run(EmailService().send_html_email(
            to_email=vendor_email,
            subject=f"BOOPPA PDPA Monitor — {month_label} Regulatory Alert",
            body_html=body_html,
        ))
    except Exception as exc:
        logger.error(f"[PDPAMonitorAlert] Email failed for {vendor_email}: {exc}")
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(bind=True, max_retries=2, name="pdpa_monitor_quarterly_rescan_task")
def pdpa_monitor_quarterly_rescan_task(self, vendor_id: str, vendor_email: str, website_url: str):
    """
    Quarterly PDPA re-scan for PDPA Monitor subscribers.
    Creates a new PDPA report and queues fulfill_pdpa_task.
    """
    db = SessionLocal()
    try:
        from app.core.models import Report, User
        import uuid as _uuid

        user = db.query(User).filter(User.id == vendor_id).first()
        company = getattr(user, "company", "Customer") if user else "Customer"

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
                "triggered_by": "pdpa_monitor_quarterly",
            },
        )
        db.add(stub)
        db.commit()
        db.refresh(stub)

        from app.api.stripe_webhook import _fulfill_pdpa
        asyncio.run(_fulfill_pdpa(report_id=str(stub.id), customer_email=vendor_email))
        logger.info(f"PDPA Monitor quarterly re-scan complete for vendor {vendor_id}")
    except Exception as exc:
        logger.error(f"PDPA Monitor quarterly re-scan failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=600)
    finally:
        db.close()


@celery_app.task(name="run_vendor_active_monthly_checks")
def run_vendor_active_monthly_checks():
    """
    Beat task: runs on the 1st of each month.
    Finds all active Vendor Active subscribers and queues health checks.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        # Query Subscription table (source of truth) for active vendor_active subs
        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_(["vendor_active_monthly", "vendor_active_annual", "vendor_active"]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
        for user in subscribers:
            if user.email:
                vendor_active_health_check_task.delay(str(user.id), user.email)
        logger.info(f"Queued monthly health checks for {len(subscribers)} Vendor Active subscribers")
    finally:
        db.close()


@celery_app.task(name="run_pdpa_monitor_quarterly_rescans")
def run_pdpa_monitor_quarterly_rescans():
    """
    Beat task: runs on the 1st of Jan, Apr, Jul, Oct.
    Finds all PDPA Monitor subscribers and queues re-scans.
    """
    db = SessionLocal()
    try:
        from app.core.models import User, Subscription as SubModel
        # Query Subscription table (source of truth) for active pdpa_monitor subs
        active_subs = db.query(SubModel).filter(
            SubModel.product_type.in_(["pdpa_monitor_monthly", "pdpa_monitor_annual", "pdpa_monitor"]),
            SubModel.status.in_(("active", "trialing")),
        ).all()
        user_ids = {s.user_id for s in active_subs if s.user_id}
        subscribers = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
        for user in subscribers:
            website = getattr(user, "website", "") or ""
            if user.email and website:
                pdpa_monitor_quarterly_rescan_task.delay(str(user.id), user.email, website)
        logger.info(f"Queued quarterly PDPA re-scans for {len(subscribers)} PDPA Monitor subscribers")
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
        from app.core.models_v10 import TenderShortlist
        db = SessionLocal()
        try:
            # ── 1. Fetch GeBIZ award data from data.gov.sg ──────────────────────
            # Dataset: Government Procurement Awards
            # Resource IDs to try in order (primary + fallback)
            GEBIZ_DATASET_IDS = [
                "d_a2c0b1c04e3e55e4e8d39f86b42b0e57",  # Government Procurement Awards
                "5ab68aac-91f6-4f39-9b21-698610bdf3f7",  # Fallback
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

            fetched_any = False
            async with httpx.AsyncClient(timeout=30) as client:
                for dataset_id in GEBIZ_DATASET_IDS:
                    offset = 0
                    while True:
                        try:
                            resp = await client.get(
                                "https://data.gov.sg/api/action/datastore_search",
                                params={
                                    "resource_id": dataset_id,
                                    "limit": PAGE_SIZE,
                                    "offset": offset,
                                },
                                headers={"User-Agent": "BooppaBot/1.0"},
                            )
                        except Exception as e:
                            logger.warning(f"[GeBIZ] Fetch error dataset={dataset_id} offset={offset}: {e}")
                            break

                        if resp.status_code != 200:
                            logger.warning(f"[GeBIZ] HTTP {resp.status_code} for dataset {dataset_id}")
                            break

                        data = resp.json()
                        records = data.get("result", {}).get("records", [])
                        if not records:
                            break

                        fetched_any = True
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
                            awarded = bool(
                                rec.get("awarded_date")
                                or rec.get("supplier_name")
                                or rec.get("award_amt")
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

                        total = data.get("result", {}).get("total", 0)
                        offset += PAGE_SIZE
                        if offset >= total:
                            break

                    if fetched_any:
                        break  # got data from first working dataset

            if not fetched_any:
                logger.warning("[GeBIZ] No data fetched from any dataset — base_rates unchanged")
                return

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
    """
    from app.services.gebiz_service import fetch_from_rss, scrape_gebiz_page

    db = SessionLocal()
    try:
        rss_count = fetch_from_rss(db)
        scrape_count = scrape_gebiz_page(db)
        logger.info(f"[GeBIZ] sync complete: rss={rss_count}, scrape={scrape_count}")

        # Bridge GebizTender → TenderShortlist so the probability engine can
        # score any RSS-synced tender without requiring a manual admin entry.
        _bridge_gebiz_to_shortlist(db)
    except Exception as exc:
        logger.error(f"[GeBIZ] sync_gebiz_tenders failed: {exc}")
        db.rollback()
    finally:
        db.close()


def _bridge_gebiz_to_shortlist(db) -> None:
    """
    Upsert open GebizTenders into TenderShortlist with a default base_rate.
    Also writes GeBizActivity rows linking vendors to open tenders in their sector.
    """
    from app.core.models_gebiz import GebizTender
    from app.core.models_v10 import TenderShortlist
    from app.core.models_v6 import GeBizActivity
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
            from app.core.models_v10 import MarketplaceVendor as Model
        else:
            from app.core.models_v10 import DiscoveredVendor as Model

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
        # Delete reports older than 30 days
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)

        old_reports = (
            db.query(Report)
            .filter(Report.status == "completed", Report.created_at < cutoff_date)
            .all()
        )

        for report in old_reports:
            # In production, you might archive instead of delete
            db.delete(report)

        db.commit()
        logger.info(f"Cleaned up {len(old_reports)} old reports")

    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed: {e}")
    finally:
        db.close()


@celery_app.task(name="send_weekly_vendor_scores")
def send_weekly_vendor_scores():
    """
    Send every active vendor their weekly compliance score summary.
    Runs every Monday at 08:00 UTC via Celery Beat.
    Non-fatal — individual email failures are logged and skipped.
    """
    from app.core.models import User
    from app.core.models_v6 import VendorScore

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
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:32px;">
                <div style="max-width:560px;margin:0 auto;">
                  <h2 style="color:#ffffff;">Your Weekly Vendor Score</h2>
                  <p>Hi {user.full_name or user.company or user.email},</p>
                  <p>Here's how your BOOPPA compliance profile performed this week:</p>
                  <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Compliance</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.compliance_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Visibility</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.visibility_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Engagement</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.engagement_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Procurement Interest</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.procurement_interest_score}</td></tr>
                    <tr style="border-top:1px solid #262626;">
                      <td style="padding:12px 0;color:#ffffff;font-weight:bold;">Total Score</td>
                      <td style="padding:12px 0;text-align:right;font-size:1.4em;font-weight:bold;color:#a78bfa;">{score.total_score}</td>
                    </tr>
                  </table>
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="display:inline-block;background:#7c3aed;color:#ffffff;padding:12px 24px;
                            border-radius:8px;text-decoration:none;font-weight:bold;margin-top:8px;">
                    View Full Dashboard
                  </a>
                  <p style="margin-top:24px;font-size:0.8em;color:#525252;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                </div>
                </body></html>
                """
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(user.email, subject, body_html))
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
    }
    label = labels.get(product_type, "your BOOPPA product")
    name = company_name or vendor_email

    body_html = f"""<html><body style="font-family:Arial;max-width:600px;margin:0 auto;color:#0f172a;">
    <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
      <h1 style="color:#10b981;margin:0;font-size:18px;">What to do next with your {label}</h1>
    </div>
    <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
      <p>Hello {name},</p>
      <ol style="line-height:2;">
        <li><strong>Add your QR badge to your email signature.</strong>
            Every email is a buyer touchpoint.</li>
        <li><strong>Check your sector percentile</strong> on your dashboard.
            Below median = add notarized documents.</li>
        <li><strong>Run the Tender Win Calculator</strong> at
            booppa.io/tender-check to see your exact win probability.</li>
      </ol>
      <p>
        <a href="https://www.booppa.io/vendor/dashboard"
           style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                  border-radius:6px;font-weight:bold;display:inline-block;">
          Go to your dashboard
        </a>
      </p>
      <p style="color:#64748b;font-size:12px;margin-top:24px;">booppa.io</p>
    </div>
    </body></html>"""

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
    from app.core.models_gebiz import GebizTender
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
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#e5e5e5;">
                <a href="{tender_url}" style="color:#a78bfa;text-decoration:none;font-weight:500;">{t.tender_no}</a><br>
                <span style="font-size:0.85em;color:#a3a3a3;">{t.title[:120]}</span>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#a3a3a3;white-space:nowrap;">{t.agency}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#60a5fa;white-space:nowrap;">{value_str}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;white-space:nowrap;">
                <span style="color:{'#ef4444' if isinstance(days_left, int) and days_left <= 3 else '#f59e0b' if isinstance(days_left, int) and days_left <= 7 else '#10b981'}">
                  {days_left}d left
                </span>
              </td>
            </tr>"""

        vendors = db.query(User).filter(User.is_active == True).all()
        email_svc = EmailService()

        for vendor in vendors:
            try:
                subject = f"GeBIZ Alert: {len(tenders)} tenders closing in the next 14 days"
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:32px;">
                <div style="max-width:640px;margin:0 auto;">
                  <p style="font-size:0.8em;color:#525252;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
                    BOOPPA · GeBIZ Intelligence
                  </p>
                  <h2 style="color:#ffffff;margin-top:0;">Tenders Closing Soon</h2>
                  <p style="color:#a3a3a3;">
                    Hi {vendor.full_name or vendor.company or vendor.email},<br>
                    Here are the GeBIZ opportunities closing within the next 14 days.
                    Check your win probability before you bid.
                  </p>

                  <table style="width:100%;border-collapse:collapse;margin:20px 0;font-size:0.9em;">
                    <thead>
                      <tr style="border-bottom:1px solid #404040;">
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Tender</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Agency</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Est. Value</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Deadline</th>
                      </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                  </table>

                  <div style="margin:24px 0;display:flex;gap:12px;">
                    <a href="https://www.booppa.io/tender-check"
                       style="display:inline-block;background:#7c3aed;color:#ffffff;padding:12px 24px;
                              border-radius:8px;text-decoration:none;font-weight:bold;">
                      Check Win Probability →
                    </a>
                    <a href="https://www.booppa.io/opportunities"
                       style="display:inline-block;background:#1a1a1a;color:#a78bfa;padding:12px 24px;
                              border-radius:8px;text-decoration:none;font-weight:bold;border:1px solid #404040;">
                      View All Open Tenders
                    </a>
                  </div>

                  <p style="margin-top:24px;font-size:0.8em;color:#525252;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                </div>
                </body></html>
                """
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(vendor.email, subject, body_html))
                sent += 1
            except Exception as exc:
                logger.warning(f"[GeBIZAlert] Failed to send to {vendor.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[GeBIZAlert] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[GeBIZAlert] Tenders={len(tenders)} Sent={sent} Failed={failed}")


@celery_app.task(name="weekly_intelligence_brief")
def weekly_intelligence_brief():
    """
    Send every vendor with a completed report their weekly intelligence brief.
    Runs Monday 00:00 UTC (08:00 SGT) via Celery Beat.
    Distinct from send_weekly_vendor_scores — this targets all vendors with any
    completed report, not just those with a VendorScore record.
    """
    from app.core.models import Report, User
    from app.core.models_v6 import VendorScore
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
                user = db.query(User).filter(User.id == owner_id).first()
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

                body_html = f"""<html><body style="font-family:Arial;max-width:600px;margin:0 auto;">
                <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                  <h1 style="color:#10b981;margin:0;font-size:16px;">Weekly intelligence brief</h1>
                </div>
                <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;color:#0f172a;">
                  <p>Trust Score: <strong>{score}/100</strong> — {position}.</p>
                  <p style="font-size:14px;color:#475569;">
                    {"Your score is strong. Consider adding notarized documents to reach DEEP verification." if score >= 60
                     else "Add PDPA Snapshot or Notarization to improve your score and move above median."}
                  </p>
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="background:#0f172a;color:#fff;padding:10px 20px;text-decoration:none;
                            border-radius:6px;font-weight:bold;display:inline-block;margin-top:16px;">
                    View dashboard
                  </a>
                  <p style="margin-top:24px;font-size:11px;color:#94a3b8;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                  </p>
                </div>
                </body></html>"""

                _asyncio.run(email_svc.send_html_email(
                    to_email=user.email,
                    subject="Your weekly BOOPPA profile brief",
                    body_html=body_html,
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
