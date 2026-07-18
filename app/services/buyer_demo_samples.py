"""Demo/test-checkout sample supplier estate.

A `livemode=false` Stripe checkout fires every buyer deliverable so a client can
see the product in one activation. But a fresh demo account has an empty
watchlist, so the Procurement Intelligence Report's comparison table and the
snapshot / drift / certificate emails render empty or against a lone placeholder
("Sample Supplier Pte Ltd") — the demo shows the frame, not the picture.

This module supplies a believable multi-supplier estate for demo mode only. The
vendor names are wholly FICTIONAL ("… Pte Ltd" with non-resolving `.example`
domains) — real, named companies must never appear next to a synthesized risk
verdict. The list is a static constant so the output is identical in every
environment with no runtime file/`openpyxl` dependency.

Scores and risk signals are **synthesized deterministically** from a hash of the
name, with variety forced so the demo always tells a story: guaranteed at least
one CRITICAL and one FLAGGED supplier (so `summarise_watchlist` reports non-zero
"need attention" / "slipped" and the alerting-names line renders), the rest
healthy/MONITORED with a mix of ▲/▼ deltas.

Nothing here is real data or advice — it exists purely to demonstrate the
artifact shapes. Live buyer flows never touch it.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

# Wholly FICTIONAL supplier estate. These names must never collide with a real,
# named company: the demo deliberately forces one supplier to CRITICAL and one to
# FLAGGED, and printing a fabricated risk verdict next to a real firm's name is a
# reputational/legal exposure even in test output (a screenshot in a deck is
# enough). Invented "Pte Ltd" names + non-resolving `.example` domains keep the
# artifact shapes believable while grounding nothing in a real entity. Demo rows
# are synthesized, never scanned, so the websites are display-only placeholders.
_DEMO_SUPPLIERS: List[tuple[str, str]] = [
    ("Meridian Logistics Pte Ltd", "https://www.meridian-logistics.example"),
    ("Harborline Freight Solutions Pte Ltd", "https://www.harborline-freight.example"),
    ("Crestwave Technologies Pte Ltd", "https://www.crestwave-tech.example"),
    ("Liongate Facilities Management Pte Ltd", "https://www.liongate-fm.example"),
    ("Marina Data Systems Pte Ltd", "https://www.marina-datasystems.example"),
    ("Orchard Point Consulting Pte Ltd", "https://www.orchardpoint-consulting.example"),
    ("Sentosa Digital Pte Ltd", "https://www.sentosa-digital.example"),
    ("Seaview Engineering Works Pte Ltd", "https://www.seaview-eng.example"),
    ("Tanglin Facilities Pte Ltd", "https://www.tanglin-facilities.example"),
    ("Raffles Quay Advisory Pte Ltd", "https://www.rafflesquay-advisory.example"),
    ("Bukit Timah Software Pte Ltd", "https://www.bukittimah-software.example"),
    ("Jurong Precision Manufacturing Pte Ltd", "https://www.jurong-precision.example"),
    ("Novena Health Services Pte Ltd", "https://www.novena-health.example"),
    ("Clementi Cloud Pte Ltd", "https://www.clementi-cloud.example"),
    ("Paya Lebar Trading Pte Ltd", "https://www.payalebar-trading.example"),
    ("Woodlands Industrial Supply Pte Ltd", "https://www.woodlands-supply.example"),
    ("Changi Aero Services Pte Ltd", "https://www.changi-aero.example"),
    ("Tampines Retail Group Pte Ltd", "https://www.tampines-retail.example"),
    ("Bugis Media Pte Ltd", "https://www.bugis-media.example"),
    ("Serangoon Analytics Pte Ltd", "https://www.serangoon-analytics.example"),
    ("Pasir Ris Marine Pte Ltd", "https://www.pasirris-marine.example"),
    ("Yishun Foodworks Pte Ltd", "https://www.yishun-foodworks.example"),
    ("Kallang Security Pte Ltd", "https://www.kallang-security.example"),
    ("Redhill Networks Pte Ltd", "https://www.redhill-networks.example"),
    ("Queenstown Robotics Pte Ltd", "https://www.queenstown-robotics.example"),
    ("Bedok Energy Pte Ltd", "https://www.bedok-energy.example"),
    ("Dover Insurance Advisory Pte Ltd", "https://www.dover-advisory.example"),
    ("Holland Village Design Pte Ltd", "https://www.hollandvillage-design.example"),
    ("Somerset Payments Pte Ltd", "https://www.somerset-payments.example"),
    ("Buona Vista Biotech Pte Ltd", "https://www.buonavista-biotech.example"),
]

# Healthy suppliers carry a non-alerting signal; MONITORED is outside
# _ALERT_RISK_SIGNALS ({"FLAGGED", "CRITICAL"}) so they don't count as alerting.
_HEALTHY_SIGNAL = "MONITORED"


def _slug(name: str) -> str:
    return "demo-" + "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


def _seed(name: str) -> int:
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16)


def _row(name: str, website: str, signal: str) -> Dict[str, Any]:
    """One watched-supplier row in the shape get_watched_suppliers_with_status emits."""
    h = _seed(name)
    # Healthy suppliers score high; flagged mid; critical low. Deltas mixed.
    if signal == "CRITICAL":
        trust = 38 + h % 12          # 38–49
        compliance = 34 + (h >> 4) % 14
        t_delta = -(6 + h % 9)       # always down
        c_delta = -(4 + (h >> 8) % 8)
    elif signal == "FLAGGED":
        trust = 58 + h % 12          # 58–69
        compliance = 55 + (h >> 4) % 13
        t_delta = -(2 + h % 6)
        c_delta = (h >> 8) % 5 - 2    # small ± swing
    else:  # healthy / MONITORED
        trust = 78 + h % 16          # 78–93
        compliance = 80 + (h >> 4) % 15
        t_delta = (h % 9) - 3        # mostly up
        c_delta = ((h >> 8) % 7) - 2
    return {
        "vendor_ref": _slug(name),
        "vendor_name": name,
        "website": website,
        "notes": None,
        "resolved": True,
        "risk_signal": signal,
        "procurement_readiness": "READY" if signal == _HEALTHY_SIGNAL else "REVIEW",
        "trust_score": int(trust),
        "compliance_score": int(compliance),
        "trust_delta": int(t_delta),
        "compliance_delta": int(c_delta),
    }


def demo_watched_suppliers(n: int = 6) -> List[Dict[str, Any]]:
    """A believable demo estate of `n` suppliers with forced signal variety.

    Guarantees ≥1 CRITICAL and ≥1 FLAGGED so the report headline cards, the
    alerting-names line, and the "needs attention" section all render with
    content. Ordering mirrors get_watched_suppliers_with_status (alerting first).
    """
    n = max(3, min(n, len(_DEMO_SUPPLIERS)))
    picked = _DEMO_SUPPLIERS[:n]
    # Slot the first two into CRITICAL / FLAGGED; the rest healthy.
    signals = ["CRITICAL", "FLAGGED"] + [_HEALTHY_SIGNAL] * (n - 2)
    rows = [_row(name, site, sig) for (name, site), sig in zip(picked, signals)]
    # Alerting suppliers first (mirrors the live insight's sort).
    rows.sort(key=lambda r: 0 if r["risk_signal"] in {"FLAGGED", "CRITICAL"} else 1)
    return rows


def demo_supplier(kind: str = "healthy") -> Dict[str, Any]:
    """A single representative demo supplier for the snapshot / drift / cert arms.

    `kind` ∈ {"critical", "flagged", "healthy"}. Deterministic per kind.
    """
    k = (kind or "healthy").lower()
    if k == "critical":
        name, site = _DEMO_SUPPLIERS[0]
        return _row(name, site, "CRITICAL")
    if k == "flagged":
        name, site = _DEMO_SUPPLIERS[1]
        return _row(name, site, "FLAGGED")
    name, site = _DEMO_SUPPLIERS[2]
    return _row(name, site, _HEALTHY_SIGNAL)
