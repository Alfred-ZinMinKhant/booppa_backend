"""
scout_vendor_pipeline.py
Booppa Smart Care LLC — SCOUT Agents, Vendor pipeline

Keeps the scoring/qualification logic from the original scout_v2_fixed.py
almost entirely as-is — it was solid engineering (real 11-dimension PDPA
proxy, real tier thresholds, correct free-scan-detail discipline). What
changes: fixed bugs (via scout_shared.py), async throughout, and the
output target — upserts ScoutProspect rows instead of writing CSV/txt
files, so the pipeline has memory across runs.
"""

import asyncio
from datetime import datetime, timezone, date
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup

from app.services.scout_shared import (
    ACRA_DATASET_ID, GEBIZ_DATASET_ID, USER_AGENT,
    fetch_all_datastore_records, normalise_company_name, prospect_natural_key,
    find_website_heuristic, check_tls_honestly, extract_contact_email,
)
from urllib.parse import urlparse

# Unchanged from the original — this part of the design was already good.
PDPA_DIMENSIONS = {
    "cookie_banner":       {"weight": 12, "label": "Cookie Consent Banner"},
    "privacy_policy":      {"weight": 15, "label": "Privacy Policy Present"},
    "privacy_policy_s13":  {"weight": 12, "label": "Privacy Policy §13 Clauses"},
    "dpo_contact":         {"weight": 10, "label": "DPO Contact Visible"},
    "ssl_active":          {"weight": 8,  "label": "SSL Certificate Active"},
    "ssl_valid":           {"weight": 7,  "label": "SSL Certificate Valid (handshake succeeds)"},
    "tracker_inventory":   {"weight": 8,  "label": "Tracker Inventory Managed"},
    "third_party_dpa":     {"weight": 8,  "label": "Third-Party DPA Referenced"},
    "retention_policy":    {"weight": 8,  "label": "Data Retention Policy"},
    "breach_contact":      {"weight": 7,  "label": "Breach Notification Contact"},
    "data_subject_rights": {"weight": 5,  "label": "Data Subject Rights Stated"},
}

SECTOR_MAP = {
    "IT":           ["technology", "software", "it services", "digital", "cyber", "cloud", "data", "systems", "solutions", "tech"],
    "Healthcare":   ["health", "medical", "clinic", "hospital", "pharma", "dental", "care", "wellness", "biotech"],
    "Finance":      ["financial", "finance", "insurance", "accounting", "audit", "advisory", "capital", "investment", "fund"],
    "Professional": ["consulting", "management", "legal", "law", "training", "education", "research", "engineering"],
    "Facilities":   ["cleaning", "facilities", "maintenance", "security", "landscaping", "pest", "waste"],
    "Construction": ["construction", "building", "civil", "infrastructure", "renovation", "architecture"],
    "Logistics":    ["logistics", "transport", "shipping", "freight", "supply chain", "courier"],
}
PDPA_RISK = {
    "IT": "HIGH", "Healthcare": "HIGH", "Finance": "HIGH",
    "Professional": "MEDIUM", "Logistics": "MEDIUM",
    "Facilities": "LOW", "Construction": "LOW",
}


def _scan_pdpa_dimensions(html: str, url: str, tls_facts: dict) -> dict:
    """Same keyword-signal scan as the original, ssl block replaced with real facts."""
    soup = BeautifulSoup(html, "html.parser")
    text_lower = html.lower()
    full_text = soup.get_text(separator=" ").lower()
    results = {}

    cookie_signals = ["cookie", "consent", "gdpr", "pdpa", "onetrust", "cookieyes", "cookie-notice", "cookie-policy"]
    results["cookie_banner"] = {"present": any(s in text_lower for s in cookie_signals), "severity": "HIGH",
                                 "finding": "Cookie consent mechanism not detected"}

    pp_signals = ["privacy policy", "privacy notice", "data protection policy", "privacy statement"]
    results["privacy_policy"] = {"present": any(s in full_text for s in pp_signals), "severity": "HIGH",
                                  "finding": "Privacy policy not found on page"}

    s13_signals = ["purpose", "collect", "retention", "how we use", "data subject", "your rights",
                   "contact us", "data protection officer"]
    s13_count = sum(1 for s in s13_signals if s in full_text)
    results["privacy_policy_s13"] = {"present": s13_count >= 4, "score": s13_count, "max": len(s13_signals),
                                      "severity": "HIGH",
                                      "finding": f"Privacy policy covers {s13_count}/{len(s13_signals)} PDPA §13 elements"}

    dpo_signals = ["dpo@", "data protection officer", "privacy@", "privacy officer", "pdpa@"]
    results["dpo_contact"] = {"present": any(s in text_lower for s in dpo_signals), "severity": "MEDIUM",
                               "finding": "No DPO contact information found"}

    # FIX #2 — real TLS facts, no fabricated grade.
    results["ssl_active"] = {"present": tls_facts["https_used"], "severity": "HIGH",
                              "finding": "Site not served over HTTPS"}
    results["ssl_valid"] = {"present": tls_facts["handshake_ok"], "severity": "MEDIUM",
                             "finding": ("TLS handshake failed — certificate may be expired, invalid, or misconfigured"
                                         if tls_facts["https_used"] else "N/A — not served over HTTPS"),
                             "days_until_expiry": tls_facts.get("days_until_expiry")}

    tracker_signals = ["google analytics", "gtag", "facebook pixel", "fbq(", "linkedin insight", "hotjar", "mixpanel"]
    trackers_found = [t for t in tracker_signals if t in text_lower]
    results["tracker_inventory"] = {"present": not trackers_found or results["cookie_banner"]["present"],
                                     "trackers_found": len(trackers_found), "severity": "HIGH",
                                     "finding": f"{len(trackers_found)} tracking scripts without confirmed consent"}

    dpa_signals = ["data processing agreement", "processor agreement", "sub-processor", "data processor"]
    results["third_party_dpa"] = {"present": any(s in full_text for s in dpa_signals), "severity": "MEDIUM",
                                   "finding": "No reference to third-party DPA or processor agreements"}

    retention_signals = ["retention period", "how long", "storage period", "data retention", "kept for"]
    results["retention_policy"] = {"present": any(s in full_text for s in retention_signals), "severity": "MEDIUM",
                                    "finding": "No data retention policy or period mentioned"}

    breach_signals = ["breach", "incident", "security incident", "data breach"]
    results["breach_contact"] = {"present": any(s in full_text for s in breach_signals), "severity": "MEDIUM",
                                  "finding": "No breach notification contact or process mentioned"}

    rights_signals = ["your rights", "right to access", "right to erasure", "data subject rights", "withdraw consent", "opt out"]
    results["data_subject_rights"] = {"present": any(s in full_text for s in rights_signals), "severity": "LOW",
                                       "finding": "Data subject rights not explicitly stated"}

    return results


def _calculate_trust_score(scan_results: dict) -> int:
    total_weight = sum(d["weight"] for d in PDPA_DIMENSIONS.values())
    score = 0
    for dim_key, dim_cfg in PDPA_DIMENSIONS.items():
        result = scan_results.get(dim_key, {})
        if dim_key == "privacy_policy_s13":
            score += dim_cfg["weight"] * (result.get("score", 0) / result.get("max", 8))
        elif result.get("present", False):
            score += dim_cfg["weight"]
    return min(100, int((score / total_weight) * 100))


def _classify_sector(name: str, gebiz_sectors: str, acra_sector: str) -> str:
    text = f"{name} {gebiz_sectors} {acra_sector}".lower()
    for sector, keywords in SECTOR_MAP.items():
        if any(k in text for k in keywords):
            return sector
    return "Other"


async def run_vendor_pipeline(limit_vendors: int = 500, min_awards: int = 2,
                                days_back: int = 365) -> list[dict]:
    """
    Returns a list of scored prospect dicts, ready for
    scout_celery_tasks.py to upsert into ScoutProspect. Does NOT touch the
    database itself — keeps this module testable in isolation.
    """
    # STEP 1 — GeBIZ download
    raw_records = await fetch_all_datastore_records(GEBIZ_DATASET_ID, max_records=limit_vendors * 20)

    # STEP 2 — aggregate by vendor
    aggregated: dict[str, dict] = {}
    for rec in raw_records:
        raw_name = (rec.get("supplier_name") or rec.get("awarded_name") or "").strip()
        if not raw_name or raw_name.upper() in ("", "UNKNOWN", "N/A"):
            continue
        key = normalise_company_name(raw_name)
        if not key:
            continue
        try:
            value = float(str(rec.get("awarded_amt") or 0).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            value = 0.0
        award_date = rec.get("award_date") or rec.get("awarded_date") or ""
        sector_hint = rec.get("tender_description", "")
        uen = (rec.get("uen") or rec.get("supplier_uen") or "").strip()

        if key not in aggregated:
            aggregated[key] = {"raw_name": raw_name, "clean_name": key.title(), "uen": uen,
                                "award_count": 0, "total_value": 0.0, "last_award_date": "", "sectors": set()}
        agg = aggregated[key]
        agg["award_count"] += 1
        agg["total_value"] += value
        if uen and not agg["uen"]:
            agg["uen"] = uen
        if award_date and (not agg["last_award_date"] or award_date > agg["last_award_date"]):
            agg["last_award_date"] = award_date
        if sector_hint:
            agg["sectors"].add(sector_hint.strip())

    candidates = [
        {**agg, "sectors": list(sorted(agg["sectors"])), "sectors_str": ", ".join(sorted(agg["sectors"]))}
        for agg in aggregated.values() if agg["award_count"] >= min_awards
    ]
    candidates.sort(key=lambda a: a["award_count"], reverse=True)
    candidates = candidates[:limit_vendors]

    # STEP 3 — ACRA verify
    for c in candidates:
        acra_rec = None
        if c["uen"]:
            recs = await fetch_all_datastore_records(ACRA_DATASET_ID, filters={"uen": c["uen"]}, max_records=1)
            acra_rec = recs[0] if recs else None
        if not acra_rec:
            recs = await fetch_all_datastore_records(ACRA_DATASET_ID, q=c["clean_name"], max_records=1)
            acra_rec = recs[0] if recs else None

        if acra_rec:
            c["uen"] = acra_rec.get("uen", c["uen"])
            c["acra_status"] = acra_rec.get("entity_status_description", "Unknown")
            c["entity_type"] = acra_rec.get("entity_type_description", "")
            c["acra_sector"] = acra_rec.get("primary_ssic_description", "")
            c["reg_date"] = acra_rec.get("uen_issue_date", "")
        else:
            c["acra_status"] = "Not Found in ACRA"
            c["entity_type"] = c["acra_sector"] = c["reg_date"] = ""

    active = [c for c in candidates if not any(
        s in c["acra_status"].upper() for s in ["STRUCK", "DISSOLVED", "CANCELLED", "WOUND", "DEREGISTERED"]
    )]

    # STEP 4 — website discovery
    for c in active:
        c["website_url"] = await find_website_heuristic(c["clean_name"])
        c["website_found"] = bool(c["website_url"])

    # STEP 5+6 — PDPA scan + trust score
    scannable = [c for c in active if c["website_found"]]
    async with httpx.AsyncClient(verify=True, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT}) as client:
        for c in scannable:
            try:
                r = await client.get(c["website_url"], timeout=15)
                html = r.text
                tls_facts = check_tls_honestly(c["website_url"])
                c["findings"] = _scan_pdpa_dimensions(html, c["website_url"], tls_facts)
                c["trust_score"] = _calculate_trust_score(c["findings"])
                c["scan_error"] = None
                domain = urlparse(c["website_url"]).netloc
                c["contact_email"] = extract_contact_email(html, domain)
            except httpx.HTTPError as e:
                c["findings"] = {}
                c["trust_score"] = 0
                c["scan_error"] = str(e)
            await asyncio.sleep(0.3)

    # STEP 7 — classify sector + priority tier
    scored = [c for c in active if c.get("website_found") and not c.get("scan_error")]
    for c in active:
        c["sector"] = _classify_sector(c["clean_name"], c.get("sectors_str", ""), c.get("acra_sector", ""))
        c["pdpa_risk"] = PDPA_RISK.get(c["sector"], "LOW")

    sector_groups: dict[str, list] = defaultdict(list)
    for c in scored:
        sector_groups[c["sector"]].append(c)
    for group in sector_groups.values():
        group.sort(key=lambda c: c["trust_score"], reverse=True)
        for rank, c in enumerate(group, 1):
            c["sector_rank"] = rank

    for c in active:
        score = c.get("trust_score", 0)
        risk = c["pdpa_risk"]
        if risk == "HIGH" and score < 50:
            c["priority_tier"] = "TIER1"
        elif risk == "HIGH" and score < 70:
            c["priority_tier"] = "TIER2"
        elif risk == "MEDIUM" and score < 50:
            c["priority_tier"] = "TIER2"
        else:
            c["priority_tier"] = "TIER3"

    for c in active:
        c["natural_key"] = prospect_natural_key(c.get("uen", ""), c["clean_name"])

    return active
