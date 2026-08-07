"""
scout_csp_pipeline.py
Booppa Smart Care LLC — SCOUT Agents, CSP pipeline

HONEST STATUS, CONFIRMED BY RESEARCH, NOT A PLACEHOLDER TO "FIX LATER":
there is no bulk-downloadable dataset of ACRA-licensed Corporate Service
Providers. Checked directly against ACRA's own site (acra.gov.sg/manage/
corporate-service-providers/overview) and data.gov.sg's dataset catalogue
— ACRA's own guidance is to use Bizfile's entity-by-entity search, not a
bulk file. The original scout_csp.py already reached this same conclusion
honestly (ACRA_CSP_RESOURCE_ID = "TODO-NESSUN-DATASET-CSP-SPECIFICO-TROVATO")
rather than faking a working pipeline — that judgement was correct, and
this rewrite keeps it rather than papering over it to look more finished.

WHAT THIS MEANS FOR AUTOMATION: this pipeline does NOT auto-fetch its own
input the way the vendor and buyer pipelines do. It runs against a
csp_seed_list you provide — a curated CSV/list of company names + UENs
(from a purchased list, a manual Bizfile export, or CSPs already known to
Booppa through other products). Everything AFTER that point (website
discovery, AML-readiness scan, scoring, outreach generation) is fully
automatable and reuses the same fixed utilities as the other two
pipelines. Only the input step is manual, and it should stay manual
until a real bulk source is confirmed — this task is deliberately NOT
added to the Celery beat schedule in scout_celery_tasks.py for that
reason; see the note there.

COST DISCIPLINE: no paid data broker was chosen as a substitute (Zephira
and similar aggregators exist and may have solved this, but are a paid
third-party service — outside the zero-additional-cost requirement, and
not evaluated here since that evaluation is a business decision, not an
engineering one).
"""

from datetime import date, datetime

from app.services.scout_shared import USER_AGENT, prospect_natural_key, find_website_heuristic, extract_contact_email
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup

AML_READINESS_DIMENSIONS = {
    "compliance_page":     {"weight": 20, "label": "Compliance/AML Page Present"},
    "aml_cft_mention":     {"weight": 18, "label": "AML/CFT Programme Referenced"},
    "cdd_mention":         {"weight": 15, "label": "CDD/EDD Process Referenced"},
    "ubo_mention":         {"weight": 12, "label": "Beneficial Ownership Disclosure Referenced"},
    "str_mention":         {"weight": 10, "label": "Suspicious Transaction Reporting Referenced"},
    "compliance_officer":  {"weight": 10, "label": "Named Compliance/MLRO Contact"},
    "acra_licence_display": {"weight": 8, "label": "ACRA Licence Number Displayed"},
    "training_mention":    {"weight": 7,  "label": "Staff Training Programme Referenced"},
}
LICENCE_AGE_TIER = {"NEW": (0, 180), "ESTABLISHED": (180, 1825), "LEGACY": (1825, None)}


def _scan_aml_dimensions(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator=" ").lower()
    results = {}

    results["compliance_page"] = {"present": any(s in full_text for s in
        ["compliance", "regulatory", "aml policy", "anti-money laundering"]), "severity": "HIGH"}
    results["aml_cft_mention"] = {"present": any(s in full_text for s in
        ["aml/cft", "anti-money laundering", "countering the financing of terrorism"]), "severity": "HIGH"}
    results["cdd_mention"] = {"present": any(s in full_text for s in
        ["customer due diligence", "cdd", "enhanced due diligence", "edd"]), "severity": "MEDIUM"}
    results["ubo_mention"] = {"present": any(s in full_text for s in
        ["beneficial owner", "ubo", "ultimate beneficial"]), "severity": "MEDIUM"}
    results["str_mention"] = {"present": any(s in full_text for s in
        ["suspicious transaction", "str filing", "suspicious activity"]), "severity": "MEDIUM"}
    results["compliance_officer"] = {"present": any(s in full_text for s in
        ["compliance officer", "mlro", "money laundering reporting officer"]), "severity": "LOW"}
    results["acra_licence_display"] = {"present": "csp licence" in full_text or "acra licence" in full_text,
                                        "severity": "LOW"}
    results["training_mention"] = {"present": any(s in full_text for s in
        ["staff training", "employee training", "aml training"]), "severity": "LOW"}

    return results


def _calculate_aml_score(scan_results: dict) -> int:
    total_weight = sum(d["weight"] for d in AML_READINESS_DIMENSIONS.values())
    score = sum(AML_READINESS_DIMENSIONS[k]["weight"] for k, v in scan_results.items() if v.get("present"))
    return min(100, int((score / total_weight) * 100))


async def run_csp_pipeline(csp_seed_list: list[dict]) -> list[dict]:
    """
    csp_seed_list: [{"name": str, "uen": str (optional), "licence_issue_date": "YYYY-MM-DD" (optional)}, ...]
    Provided by the caller (see scout_celery_tasks.py's manual-trigger
    endpoint) — never auto-fetched, per the honest limitation above.
    """
    csps = []
    today = date.today()

    for seed in csp_seed_list:
        c = dict(seed)
        c["clean_name"] = seed["name"].strip()
        c["uen"] = seed.get("uen", "")

        issue_date_str = seed.get("licence_issue_date", "")
        age_days = -1
        if issue_date_str:
            try:
                issue = datetime.strptime(issue_date_str[:10], "%Y-%m-%d").date()
                age_days = (today - issue).days
            except (ValueError, TypeError):
                pass
        c["licence_age_days"] = age_days
        c["licence_age_tier"] = "UNKNOWN"
        for tier, (lo, hi) in LICENCE_AGE_TIER.items():
            if age_days < 0:
                break
            if hi is None and age_days >= lo:
                c["licence_age_tier"] = tier
                break
            elif hi is not None and lo <= age_days < hi:
                c["licence_age_tier"] = tier
                break

        c["website_url"] = await find_website_heuristic(c["clean_name"])
        c["website_found"] = bool(c["website_url"])
        c["natural_key"] = prospect_natural_key(c["uen"], c["clean_name"])
        csps.append(c)

    scannable = [c for c in csps if c["website_found"]]
    async with httpx.AsyncClient(verify=True, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT}) as client:
        for c in scannable:
            try:
                r = await client.get(c["website_url"], timeout=15)
                c["findings"] = _scan_aml_dimensions(r.text)
                c["aml_readiness_score"] = _calculate_aml_score(c["findings"])
                c["scan_error"] = None
                domain = urlparse(c["website_url"]).netloc
                c["contact_email"] = extract_contact_email(r.text, domain)
            except httpx.HTTPError as e:
                c["findings"] = {}
                c["aml_readiness_score"] = 0
                c["scan_error"] = str(e)

    for c in csps:
        if not c.get("website_found") or c.get("scan_error"):
            c["priority_tier"] = "TIER3"
            continue
        score = c["aml_readiness_score"]
        if c["licence_age_tier"] == "NEW" and score < 50:
            c["priority_tier"] = "TIER1"
        elif score < 30:
            c["priority_tier"] = "TIER1"
        elif score < 60:
            c["priority_tier"] = "TIER2"
        else:
            c["priority_tier"] = "TIER3"

    return csps
