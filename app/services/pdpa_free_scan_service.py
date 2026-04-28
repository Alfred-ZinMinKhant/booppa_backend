"""
PDPA Free Scan Service
======================
Lightweight HTTP-based PDPA compliance checks — no AI, no blockchain.
Returns structured findings for the free teaser scan.
"""

import httpx
import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

import time

TIMEOUT = 15  # seconds per request
LOADING_RETRY_DELAY = 30  # seconds to wait before retrying after loading screen
MAX_LOADING_RETRIES = 2   # retry up to 2 times (30s + 30s = ~1 min total wait)

# Patterns that indicate a bot-challenge / interstitial page
LOADING_SCREEN_PATTERNS = [
    "please wait", "loading...", "just a moment", "checking your browser",
    "one moment please", "verifying you are human", "please enable javascript",
    "attention required", "ray id", "ddos protection",
]

# SPA framework markers — if present, the page is real even with little visible text
_SPA_SIGNALS = [
    "__next", "__nuxt", "react-root", "app-root", "ng-app",
    "<script src=", "<link rel=\"stylesheet\"", "<meta name=",
    "<!doctype html>",
]

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _is_loading_screen(html: str) -> bool:
    """Detect bot-challenge / interstitial pages.

    Must NOT flag legitimate SPA shells (React, Next.js, Nuxt) that have
    minimal visible text but are real pages.
    """
    if not html or len(html.strip()) < 100:
        return True  # truly empty
    html_lower = html.lower()

    # SPA framework pages are real even with sparse visible text
    if any(sig in html_lower for sig in _SPA_SIGNALS):
        return False

    # Cloudflare challenge page
    if "cloudflare" in html_lower and ("ray id" in html_lower or "challenge" in html_lower):
        return True

    # Other cases: require both sparse text AND loading keywords
    body_match = re.search(r"<body[^>]*>(.*)</body>", html_lower, re.DOTALL)
    body_text = body_match.group(1) if body_match else html_lower
    visible_text = re.sub(r"<[^>]+>", "", body_text).strip()
    if len(visible_text) < 150:
        if any(p in html_lower for p in LOADING_SCREEN_PATTERNS):
            return True
    return False


def _severity_weight(severity: str) -> int:
    return {"CRITICAL": 30, "HIGH": 15, "MEDIUM": 5, "LOW": 2}.get(severity, 0)


def _check_https(url: str) -> dict | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {
            "check_id": "https",
            "title": "Website does not use HTTPS",
            "severity": "CRITICAL",
            "category": "Transport Security",
            "description": (
                "Your website is served over unencrypted HTTP. All personal data "
                "transmitted between users and your site can be intercepted. "
                "This violates PDPA Section 24 (Protection Obligation)."
            ),
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Obtain an SSL/TLS certificate and enforce HTTPS across all pages.",
        }
    return None


def _check_headers(headers: dict) -> list[dict]:
    findings = []

    if "strict-transport-security" not in headers:
        findings.append({
            "check_id": "hsts",
            "title": "Missing HSTS header",
            "severity": "HIGH",
            "category": "Transport Security",
            "description": (
                "HTTP Strict-Transport-Security header is missing. Browsers may "
                "allow insecure HTTP connections, exposing personal data in transit."
            ),
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Add Strict-Transport-Security header with a minimum max-age of 31536000.",
        })

    if "content-security-policy" not in headers:
        findings.append({
            "check_id": "csp",
            "title": "Missing Content Security Policy",
            "severity": "HIGH",
            "category": "Security Headers",
            "description": (
                "No Content-Security-Policy header found. Your site is vulnerable to "
                "cross-site scripting (XSS) attacks that could exfiltrate personal data."
            ),
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Implement a Content-Security-Policy header restricting script sources.",
        })

    if "x-frame-options" not in headers:
        findings.append({
            "check_id": "x_frame",
            "title": "Missing X-Frame-Options header",
            "severity": "MEDIUM",
            "category": "Security Headers",
            "description": (
                "Your site can be embedded in iframes on other domains, "
                "making it susceptible to clickjacking attacks."
            ),
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Add X-Frame-Options: DENY or SAMEORIGIN header.",
        })

    if "x-content-type-options" not in headers:
        findings.append({
            "check_id": "x_content_type",
            "title": "Missing X-Content-Type-Options header",
            "severity": "LOW",
            "category": "Security Headers",
            "description": "Browser MIME-type sniffing is not disabled, which could lead to security issues.",
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Add X-Content-Type-Options: nosniff header.",
        })

    if "referrer-policy" not in headers:
        findings.append({
            "check_id": "referrer",
            "title": "Missing Referrer-Policy header",
            "severity": "MEDIUM",
            "category": "Data Leakage",
            "description": (
                "Without a Referrer-Policy, full URLs (which may contain personal data "
                "in query parameters) are sent to third-party sites via the Referer header."
            ),
            "legislation": "PDPA Section 18 — Purpose Limitation Obligation",
            "action": "Add Referrer-Policy: strict-origin-when-cross-origin or no-referrer.",
        })

    return findings


def _check_cookies(headers: dict) -> list[dict]:
    findings = []
    set_cookie_values = headers.get("set-cookie", "")
    if not set_cookie_values:
        return findings

    cookies_str = set_cookie_values if isinstance(set_cookie_values, str) else str(set_cookie_values)

    # Check for tracking cookies without consent
    tracking_patterns = ["_ga", "_fbp", "_gcl", "hubspot", "_hjid"]
    has_tracking = any(p in cookies_str.lower() for p in tracking_patterns)
    if has_tracking:
        findings.append({
            "check_id": "tracking_cookies",
            "title": "Tracking cookies set without explicit consent",
            "severity": "HIGH",
            "category": "Cookie Consent",
            "description": (
                "Third-party tracking cookies (analytics/advertising) are set on page load "
                "before obtaining user consent. This violates PDPA consent requirements."
            ),
            "legislation": "PDPA Section 13 — Consent Obligation",
            "action": "Defer tracking cookies until the user provides affirmative consent.",
        })

    if "secure" not in cookies_str.lower():
        findings.append({
            "check_id": "cookie_secure",
            "title": "Cookies missing Secure flag",
            "severity": "MEDIUM",
            "category": "Cookie Security",
            "description": "Some cookies are not marked as Secure, allowing transmission over HTTP.",
            "legislation": "PDPA Section 24 — Protection Obligation",
            "action": "Set the Secure flag on all cookies containing personal or session data.",
        })

    return findings


def _check_body(html: str) -> list[dict]:
    findings = []
    html_lower = html.lower()

    # Cookie consent banner detection
    # Covers: known SaaS platforms (CDN URLs + class names), WP plugins,
    # inline script references, and common plain-text phrases.
    consent_patterns = [
        # Platform class/id names
        "cookieconsent", "cookie-consent", "cookie-banner", "cookie_banner",
        "cookie-notice", "cc-banner", "cc-window", "cc-nb",
        "consent-manager", "consent-banner", "consent-dialog",
        # SaaS platforms (names appear in CDN script src or inline JS)
        "onetrust", "cookielaw.org", "cookiebot", "consent.cookiebot",
        "termly", "cookieyes", "cdn.cookieyes", "osano", "cdn.osano",
        "iubenda", "cdn.iubenda", "consentmanager", "usercentrics",
        "app.usercentrics", "didomi", "sdk.privacy-center.org",
        "trustarc", "consent.truste.com", "quantcast", "quantcast.mgr",
        "optanon", "evidon", "civic", "cookiecontrol",
        "booppa-cookie", "booppa_consent",
        # WordPress / common CMS plugins
        "cookie-law-info", "wp-cookie", "moove_gdpr", "gdpr-cookie",
        "cookies-eu-banner", "real-cookie-banner", "borlabs-cookie",
        "wt-cli", "cookie-script", "wpgdprc",
        # Common plain-text phrases (appear in banner copy)
        "we use cookies", "this site uses cookies", "this website uses cookies",
        "accept cookies", "allow cookies", "accept all cookies",
        "reject cookies", "decline cookies",
        "cookie preferences", "cookie preference", "cookie choices",
        "cookie settings", "cookie-settings", "cookies-settings",
        "manage cookies", "manage your cookies",
        # Generic consent/PDPA text
        "gdpr", "pdpa consent", "pdpa compliant", "privacy settings",
        "data-cookieconsent", "data-cc=",
    ]
    has_consent = any(p in html_lower for p in consent_patterns)
    if not has_consent:
        findings.append({
            "check_id": "no_consent_banner",
            "title": "No cookie consent mechanism detected",
            "severity": "HIGH",
            "category": "Cookie Consent",
            "description": (
                "No cookie consent banner or management platform was detected. "
                "Under PDPA, organisations must obtain consent before collecting personal data "
                "via cookies and similar technologies."
            ),
            "legislation": "PDPA Section 13 — Consent Obligation",
            "action": "Implement a cookie consent banner that blocks non-essential cookies until consent is given.",
        })

    # Privacy policy detection
    privacy_patterns = [
        'href="/privacy"', 'href="/privacy-policy"', 'href="/data-protection"',
        "privacy policy", "data protection policy", "personal data protection",
    ]
    has_privacy = any(p in html_lower for p in privacy_patterns)
    if not has_privacy:
        findings.append({
            "check_id": "no_privacy_policy",
            "title": "Privacy policy not linked on homepage",
            "severity": "HIGH",
            "category": "Transparency",
            "description": (
                "No link to a privacy or data protection policy was found on the homepage. "
                "PDPA requires organisations to make their data protection policies available."
            ),
            "legislation": "PDPA Section 11 — Openness Obligation",
            "action": "Add a clearly visible link to your privacy/data protection policy in the footer.",
        })

    # DPO contact detection
    dpo_patterns = [
        "data protection officer", "dpo@", "data.protection@",
        "privacy@", "pdpa@",
    ]
    has_dpo = any(p in html_lower for p in dpo_patterns)
    if not has_dpo:
        findings.append({
            "check_id": "no_dpo_contact",
            "title": "DPO contact not publicly disclosed on website",
            "severity": "MEDIUM",
            "category": "DPO Compliance",
            "description": (
                "No Data Protection Officer (DPO) contact information was found on the "
                "publicly accessible pages of this website. Note: this does not necessarily "
                "mean a DPO has not been appointed — the organisation may have a DPO who is "
                "not disclosed online. However, PDPA Section 11(3) requires organisations to "
                "make the business contact information of their DPO publicly available so that "
                "individuals can contact them regarding data protection matters."
            ),
            "legislation": "PDPA Section 11(3) — Openness Obligation (DPO Disclosure)",
            "action": (
                "Publish your DPO's business contact information (e.g., email address) on "
                "the website, typically in the privacy policy or site footer. If a DPO has "
                "not yet been appointed, designate one under PDPA Section 11(3)."
            ),
        })

    # NRIC collection detection
    nric_patterns = [
        r'nric', r'national registration', r'fin number', r'identity.?card.?number',
    ]
    has_nric_field = any(re.search(p, html_lower) for p in nric_patterns)
    if has_nric_field:
        findings.append({
            "check_id": "nric_collection",
            "title": "Possible NRIC/FIN collection detected",
            "severity": "CRITICAL",
            "category": "NRIC Advisory",
            "description": (
                "References to NRIC or FIN number collection were found on the website. "
                "Under the PDPA NRIC Advisory, organisations must not collect, use, or disclose "
                "NRIC numbers unless required by law or necessary to verify identity to a high "
                "degree of fidelity."
            ),
            "legislation": "PDPA Advisory Guidelines on NRIC Numbers (1 Sep 2019)",
            "action": "Review all NRIC/FIN collection points and remove unless legally required.",
        })

    return findings


def run_free_scan(website_url: str) -> dict[str, Any]:
    """
    Run a lightweight PDPA compliance scan on the given website URL.
    Returns structured findings without AI analysis.
    """
    # Normalise URL
    if not website_url.startswith(("http://", "https://")):
        website_url = f"https://{website_url}"

    findings: list[dict] = []

    # Check HTTPS
    https_finding = _check_https(website_url)
    if https_finding:
        findings.append(https_finding)

    # Fetch the page — try browser-like headers first, fall back to bot UA.
    # If a loading/splash screen is detected, wait and retry (some sites show
    # animated intros with logos for 10-30s before rendering real content).
    html = ""
    response_headers: dict = {}
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, verify=False) as client:
            # First attempt with browser-like headers (avoids 403 from WAFs)
            resp = client.get(website_url, headers=_BROWSER_HEADERS)

            # If 403, retry with different accept header / no bot UA
            if resp.status_code == 403:
                logger.info(f"Got 403 for {website_url}, retrying with alternate headers")
                alt_headers = {**_BROWSER_HEADERS, "Accept": "*/*"}
                resp = client.get(website_url, headers=alt_headers)

            if resp.status_code == 403:
                findings.append({
                    "check_id": "forbidden",
                    "title": "Website returned 403 Forbidden",
                    "severity": "MEDIUM",
                    "category": "Availability",
                    "description": (
                        "The website blocked our scanner with a 403 Forbidden response. "
                        "This may be due to a Web Application Firewall (WAF), Cloudflare protection, "
                        "or geo-blocking. The scan results below are based on limited information."
                    ),
                    "legislation": "N/A",
                    "action": "If you own this website, whitelist the scanner or provide direct access.",
                })

            response_headers = {k.lower(): v for k, v in resp.headers.items()}
            html = resp.text

            # Detect loading/splash screens and retry after delay
            if _is_loading_screen(html) and resp.status_code < 400:
                for attempt in range(1, MAX_LOADING_RETRIES + 1):
                    logger.info(
                        f"Loading screen detected for {website_url}, "
                        f"waiting {LOADING_RETRY_DELAY}s before retry {attempt}/{MAX_LOADING_RETRIES}"
                    )
                    time.sleep(LOADING_RETRY_DELAY)
                    resp = client.get(website_url, headers=_BROWSER_HEADERS)
                    response_headers = {k.lower(): v for k, v in resp.headers.items()}
                    html = resp.text
                    if not _is_loading_screen(html):
                        logger.info(f"Real content received on retry {attempt} for {website_url}")
                        break
                else:
                    # Still a loading screen after all retries
                    logger.info(f"Still loading screen after {MAX_LOADING_RETRIES} retries for {website_url}")
                    findings.append({
                        "check_id": "loading_screen",
                        "title": "Loading or splash screen detected",
                        "severity": "LOW",
                        "category": "Scan Limitation",
                        "description": (
                            "The website shows a loading screen, animated intro, or JavaScript-only "
                            "splash page that did not resolve after waiting ~1 minute. Some compliance "
                            "checks may be incomplete. A full scan with browser rendering is recommended."
                        ),
                        "legislation": "N/A",
                        "action": "Consider upgrading to a full PDPA Quick Scan which uses browser-based rendering.",
                    })
    except httpx.TimeoutException:
        findings.append({
            "check_id": "timeout",
            "title": "Website did not respond in time",
            "severity": "MEDIUM",
            "category": "Availability",
            "description": f"The website did not respond within {TIMEOUT} seconds.",
            "legislation": "N/A",
            "action": "Ensure your website is accessible and performant.",
        })
        # Return partial results
        score = min(100, sum(_severity_weight(f["severity"]) for f in findings))
        return _build_response(website_url, findings, score)
    except Exception as e:
        logger.warning(f"Free scan failed for {website_url}: {e}")
        findings.append({
            "check_id": "unreachable",
            "title": "Website is unreachable",
            "severity": "HIGH",
            "category": "Availability",
            "description": f"Could not connect to {website_url}.",
            "legislation": "N/A",
            "action": "Verify the URL and ensure the website is online.",
        })
        score = min(100, sum(_severity_weight(f["severity"]) for f in findings))
        return _build_response(website_url, findings, score)

    # Run checks
    findings.extend(_check_headers(response_headers))
    findings.extend(_check_cookies(response_headers))
    findings.extend(_check_body(html))

    # Sort by severity (CRITICAL first)
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: severity_order.get(f["severity"], 4))

    # Calculate risk score (0-100)
    score = min(100, sum(_severity_weight(f["severity"]) for f in findings))

    return _build_response(website_url, findings, score)


def _build_response(website_url: str, findings: list[dict], score: int) -> dict[str, Any]:
    if score >= 60:
        risk_level = "High Risk"
    elif score >= 30:
        risk_level = "Medium Risk"
    else:
        risk_level = "Low Risk"

    # First finding is free, rest are locked
    free_finding = findings[0] if findings else None
    locked_findings = [
        {"severity": f["severity"], "category": f["category"], "title": f["title"]}
        for f in findings[1:]
    ]

    return {
        "website_url": website_url,
        "score": score,
        "risk_level": risk_level,
        "total_findings": len(findings),
        "free_finding": free_finding,
        "locked_findings": locked_findings,
        "unlock_cta": {
            "product_type": "pdpa_quick_scan",
            "price": "SGD 79",
            "description": "Full AI-powered scan with blockchain-anchored evidence report",
        },
    }
