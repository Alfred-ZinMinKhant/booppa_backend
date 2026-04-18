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

TIMEOUT = 10  # seconds


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
    consent_patterns = [
        "cookieconsent", "cookie-consent", "cookie-banner", "cookie_banner",
        "cookie-notice", "gdpr", "onetrust", "termly", "cookiebot",
        "cc-banner", "cc-window", "consent-manager",
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
            "title": "No Data Protection Officer contact found",
            "severity": "MEDIUM",
            "category": "DPO Compliance",
            "description": (
                "No Data Protection Officer contact information was found on the website. "
                "Organisations processing personal data should designate a DPO and make "
                "their contact details accessible."
            ),
            "legislation": "PDPA Section 11(3) — DPO Designation",
            "action": "Publish your DPO's contact email on the website (e.g., in the privacy policy or footer).",
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

    # Fetch the page
    html = ""
    response_headers: dict = {}
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, verify=False) as client:
            resp = client.get(website_url, headers={"User-Agent": "BooppaPDPAScanner/1.0"})
            response_headers = {k.lower(): v for k, v in resp.headers.items()}
            html = resp.text
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
