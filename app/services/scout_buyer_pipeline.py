"""
scout_buyer_pipeline.py
Booppa Smart Care LLC — SCOUT Agents, Buyer (procuring agency) pipeline

Same fixes as scout_vendor_pipeline.py (real ACRA/GeBIZ IDs via
scout_shared, async, verify=True, DB-ready output instead of CSV). The
original scout_buyer.py's own logic was sound and its self-declared
limitations were accurate and worth keeping verbatim: no measurable
"readiness score" exists for an agency, priority_tier here means
inferred fit probability from procurement volume, not confirmed
readiness — every output and every email using this pipeline's data
must keep communicating it that way.
"""

from app.services.scout_shared import GEBIZ_DATASET_ID, USER_AGENT, prospect_natural_key, find_website_heuristic, fetch_all_datastore_records

VOLUME_TIER_THRESHOLDS = {
    "BUYER_ENTERPRISE_FIT": 50,
    "BUYER_PROFESSIONAL_FIT": 15,
    "BUYER_ESSENTIALS_FIT": 3,
}
REGULATED_SECTOR_HINTS = ["finance", "banking", "insurance", "healthcare", "health", "hospital"]


async def run_buyer_pipeline(limit_agencies: int = 200, min_awards: int = 3) -> list[dict]:
    raw_records = await fetch_all_datastore_records(GEBIZ_DATASET_ID, max_records=limit_agencies * 30)

    agencies: dict[str, dict] = {}
    for rec in raw_records:
        agency = (rec.get("agency") or "").strip()
        if not agency:
            continue
        vendor = rec.get("supplier_name", "")
        sector = rec.get("tender_description", "")
        try:
            value = float(str(rec.get("awarded_amt", rec.get("contract_value", 0)) or 0)
                          .replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            value = 0.0

        if agency not in agencies:
            agencies[agency] = {"agency_name": agency, "award_count": 0, "total_value_sgd": 0.0,
                                 "vendors": set(), "sectors": set()}
        a = agencies[agency]
        a["award_count"] += 1
        a["total_value_sgd"] += value
        if vendor:
            a["vendors"].add(vendor)
        if sector:
            a["sectors"].add(sector)

    result = []
    for agency, a in agencies.items():
        if a["award_count"] < min_awards:
            continue
        a["avg_value_sgd"] = a["total_value_sgd"] / a["award_count"]
        a["distinct_vendors"] = len(a["vendors"])
        a["sectors_awarded"] = ", ".join(sorted(a["sectors"]))
        a["regulated_signal"] = any(
            h in a["sectors_awarded"].lower() or h in agency.lower() for h in REGULATED_SECTOR_HINTS
        )
        result.append(a)

    result.sort(key=lambda a: a["award_count"], reverse=True)
    result = result[:limit_agencies]

    for a in result:
        if a["award_count"] >= VOLUME_TIER_THRESHOLDS["BUYER_ENTERPRISE_FIT"]:
            a["fit_tier"] = "BUYER_ENTERPRISE_FIT"
            a["priority_tier"] = "TIER1"
        elif a["award_count"] >= VOLUME_TIER_THRESHOLDS["BUYER_PROFESSIONAL_FIT"]:
            a["fit_tier"] = "BUYER_PROFESSIONAL_FIT"
            a["priority_tier"] = "TIER1" if a["distinct_vendors"] >= 10 else "TIER2"
        elif a["award_count"] >= VOLUME_TIER_THRESHOLDS["BUYER_ESSENTIALS_FIT"]:
            a["fit_tier"] = "BUYER_ESSENTIALS_FIT"
            a["priority_tier"] = "TIER2"
        else:
            a["fit_tier"] = "BELOW_THRESHOLD"
            a["priority_tier"] = "TIER3"
        if a["regulated_signal"] and a["priority_tier"] == "TIER2":
            a["priority_tier"] = "TIER1"

        # .gov.sg pattern first — these are Singapore government agencies, not commercial companies
        a["website_url"] = await find_website_heuristic(
            agency, extra_strip_words=("ministry", "of", "agency", "authority", "board", "council"),
            tld_patterns=(".gov.sg", ".com.sg"),
        )
        a["website_found"] = bool(a["website_url"])
        a["natural_key"] = prospect_natural_key("", agency)  # agencies have no UEN in this dataset — name-keyed

    return result
