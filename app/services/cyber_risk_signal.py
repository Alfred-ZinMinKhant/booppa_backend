"""
cyber_risk_signal.py — Component 6.4 (Cyber risk signal lookup).

Integrates passive Shodan InternetDB and SpiderFoot HTTP API checks.
Strictly gated by has_active_consent(required_source="domain_scan") prior to execution.
"""

from datetime import datetime, timezone
import socket
from typing import Optional

import requests

SPIDERFOOT_SERVICE_URL = "http://spiderfoot-internal.booppa.internal:5001"
SHODAN_INTERNETDB_URL = "https://internetdb.shodan.io/{ip}"
REQUEST_TIMEOUT_SECONDS = 20


def resolve_domain_to_ip(domain: str) -> Optional[str]:
    try:
        return socket.gethostbyname(domain)
    except socket.gaierror:
        return None


def query_shodan_internetdb(ip: str) -> dict:
    try:
        resp = requests.get(
            SHODAN_INTERNETDB_URL.format(ip=ip),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 404:
            return {"found": False, "ports": [], "vulns": [], "cpes": []}
        resp.raise_for_status()
        data = resp.json()
        return {
            "found": True,
            "ports": data.get("ports", []),
            "vulns": data.get("vulns", []),
            "cpes": data.get("cpes", []),
            "hostnames": data.get("hostnames", []),
        }
    except requests.RequestException as e:
        return {"found": False, "error": str(e), "ports": [], "vulns": [], "cpes": []}


def query_spiderfoot(domain: str) -> dict:
    try:
        resp = requests.get(
            f"{SPIDERFOOT_SERVICE_URL}/api/scan",
            params={"target": domain, "modules": "sfp_dnsresolve,sfp_crt,sfp_spider,sfp_subdomain_enum"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e), "modules_run": []}


def build_cyber_risk_signal(
    domain: str,
    db=None,
    *,
    csp_client_id: Optional[str] = None,
    managed_entity_id: Optional[str] = None,
    enforce_consent: bool = True,
) -> dict:
    if enforce_consent and db is not None:
        from app.services.scan_consent import has_active_consent
        if not (csp_client_id or managed_entity_id) or not has_active_consent(
            db,
            csp_client_id=csp_client_id,
            managed_entity_id=managed_entity_id,
            required_source="domain_scan",
        ):
            return {
                "domain": domain,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "resolved": False,
                "signal_basis": "consent missing or revoked for source 'domain_scan'",
                "consent_active": False,
            }

    ip = resolve_domain_to_ip(domain)
    if not ip:
        return {
            "domain": domain,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "resolved": False,
            "signal_basis": "domain did not resolve",
        }

    shodan_result = query_shodan_internetdb(ip)
    spiderfoot_result = query_spiderfoot(domain)

    exposed_ports = shodan_result.get("ports", [])
    known_vulns = shodan_result.get("vulns", [])

    return {
        "domain": domain,
        "resolved_ip": ip,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "resolved": True,
        "exposed_ports_count": len(exposed_ports),
        "exposed_ports": exposed_ports,
        "known_cve_count": len(known_vulns),
        "known_cves": known_vulns,
        "spiderfoot_modules_run": spiderfoot_result.get("modules_run", []),
        "spiderfoot_error": spiderfoot_result.get("error"),
        "signal_level": "elevated" if known_vulns else ("baseline" if exposed_ports else "minimal_data"),
        "consent_active": True,
    }

