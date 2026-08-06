"""
mas_tprm_export.py — MAS TPRM Exporter.

Exports vendor portfolio compliance data formatted to MAS TPRM guidelines.
"""

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import List, Optional


class Materiality(str, Enum):
    MATERIAL = "material"
    NON_MATERIAL = "non_material"
    UNCLASSIFIED = "unclassified"


@dataclass
class VendorArrangement:
    vendor_name: str
    vendor_uen: str = ""
    service_description: str = ""
    contract_start_date: Optional[date] = None
    materiality: Materiality = Materiality.UNCLASSIFIED
    materiality_basis: str = ""
    provider_concentration_pct: Optional[float] = None
    service_delivery_country: str = ""
    data_hosting_country: str = ""
    last_verification_date: Optional[date] = None
    evidence_reference: str = ""
    verification_method: str = ""
    business_owner: str = ""
    risk_rating: str = ""
    buyer_org_id: str = ""
    register_period: str = ""


def calculate_provider_concentration(
    arrangements: List[VendorArrangement],
    group_by: str = "service_description",
) -> List[VendorArrangement]:
    from collections import defaultdict

    groups: dict[str, list[VendorArrangement]] = defaultdict(list)
    for a in arrangements:
        key = getattr(a, group_by, "") or "uncategorized"
        groups[key].append(a)

    for key, group in groups.items():
        total = len(group)
        vendor_counts: dict[str, int] = defaultdict(int)
        for a in group:
            vendor_counts[a.vendor_name] += 1
        for a in group:
            if total < 2:
                a.provider_concentration_pct = None
            else:
                a.provider_concentration_pct = round(100 * vendor_counts[a.vendor_name] / total, 1)

    return arrangements


def classify_materiality(
    arrangement: VendorArrangement,
    critical_service_keywords: Optional[List[str]] = None,
    concentration_threshold_pct: float = 30.0,
) -> VendorArrangement:
    keywords = critical_service_keywords or [
        "data processing", "payment", "payments", "infrastructure",
        "authentication", "kyc", "aml", "cloud hosting", "core banking",
    ]
    desc_lower = (arrangement.service_description or "").lower()

    reasons = []
    if any(kw in desc_lower for kw in keywords):
        reasons.append("critical function keyword match")
    if (
        arrangement.provider_concentration_pct is not None
        and arrangement.provider_concentration_pct >= concentration_threshold_pct
    ):
        reasons.append(f"provider concentration >= {concentration_threshold_pct}%")

    if reasons:
        arrangement.materiality = Materiality.MATERIAL
        arrangement.materiality_basis = "; ".join(reasons)
    else:
        arrangement.materiality = Materiality.NON_MATERIAL
        arrangement.materiality_basis = "no critical keyword match, concentration below threshold"

    return arrangement


def from_vendor_records(
    vendor_records: List[dict], buyer_org_id: str, register_period: str
) -> List[VendorArrangement]:
    arrangements = []
    for r in vendor_records:
        arrangements.append(
            VendorArrangement(
                vendor_name=r.get("vendor_name", ""),
                vendor_uen="",
                service_description=r.get("label", ""),
                contract_start_date=r.get("added_at"),
                service_delivery_country="Singapore",
                data_hosting_country="",
                last_verification_date=r.get("vendor_compliance_updated_at") or r.get("cache_refreshed_at"),
                evidence_reference="",
                verification_method=r.get("cached_verification_depth", ""),
                business_owner="",
                risk_rating=r.get("cached_risk_signal", ""),
                buyer_org_id=buyer_org_id,
                register_period=register_period,
            )
        )

    arrangements = calculate_provider_concentration(arrangements)
    for a in arrangements:
        classify_materiality(a)

    return arrangements


REGISTER_FIELDS = [
    "vendor_name", "vendor_uen", "service_description", "contract_start_date",
    "materiality", "materiality_basis", "provider_concentration_pct",
    "service_delivery_country", "data_hosting_country",
    "last_verification_date", "evidence_reference", "verification_method",
    "business_owner", "risk_rating", "buyer_org_id", "register_period",
]


def _serialize(arrangement: VendorArrangement) -> dict:
    d = asdict(arrangement)
    d["materiality"] = arrangement.materiality.value
    for date_field in ("contract_start_date", "last_verification_date"):
        if d.get(date_field):
            d[date_field] = d[date_field].isoformat() if hasattr(d[date_field], "isoformat") else str(d[date_field])
    return d


def export_register_csv(arrangements: List[VendorArrangement], output_path: str) -> str:
    rows = [_serialize(a) for a in arrangements]
    rows.sort(key=lambda r: (r["materiality"] != "material", r["vendor_name"]))

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REGISTER_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return output_path


def export_register_json(arrangements: List[VendorArrangement], output_path: str) -> str:
    rows = [_serialize(a) for a in arrangements]
    rows.sort(key=lambda r: (r["materiality"] != "material", r["vendor_name"]))

    payload = {
        "register_period": arrangements[0].register_period if arrangements else "",
        "generated_note": (
            "Format aligned to MAS TPRM consultation (6 March 2026). "
            "Final MAS guidelines subject to revision once issued."
        ),
        "material_count": sum(1 for r in rows if r["materiality"] == "material"),
        "total_count": len(rows),
        "arrangements": rows,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    return output_path
