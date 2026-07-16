"""
DNS Security Service
====================
Checks a domain's email security posture (MX, SPF, DMARC) via DNS records.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
import dns.asyncresolver
import dns.resolver

logger = logging.getLogger(__name__)

async def fetch_dns_security(domain: str) -> dict[str, Any]:
    """Fetch DNS records to determine email security posture."""
    if not domain:
        return {"checked": False, "error": "No domain provided"}
    
    # Clean domain (strip protocols, paths)
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    
    result = {
        "checked": True,
        "domain": domain,
        "mx_found": False,
        "spf_record": None,
        "dmarc_record": None,
        "dmarc_policy": "none",
        "error": None
    }
    
    try:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 5
        
        # 1. Check MX records
        try:
            mx_answers = await resolver.resolve(domain, 'MX')
            if mx_answers:
                result["mx_found"] = True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, Exception):
            pass
            
        # 2. Check SPF (TXT records on root domain)
        try:
            txt_answers = await resolver.resolve(domain, 'TXT')
            for rdata in txt_answers:
                txt = b"".join(rdata.strings).decode('utf-8')
                if txt.startswith("v=spf1"):
                    result["spf_record"] = txt
                    break
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, Exception):
            pass
            
        # 3. Check DMARC (TXT records on _dmarc sub-domain)
        dmarc_domain = f"_dmarc.{domain}"
        try:
            dmarc_answers = await resolver.resolve(dmarc_domain, 'TXT')
            for rdata in dmarc_answers:
                txt = b"".join(rdata.strings).decode('utf-8')
                if txt.startswith("v=DMARC1"):
                    result["dmarc_record"] = txt
                    # parse policy
                    parts = txt.split(";")
                    for p in parts:
                        p = p.strip()
                        if p.startswith("p="):
                            result["dmarc_policy"] = p.split("=")[1].strip()
                    break
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, Exception):
            pass
            
    except Exception as e:
        logger.warning(f"Failed to fetch DNS security for {domain}: {e}")
        result["error"] = str(e)
        
    return result
