"""Demo/test-checkout sample supplier estate.

A `livemode=false` Stripe checkout fires every buyer deliverable so a client can
see the product in one activation. But a fresh demo account has an empty
watchlist, so the Procurement Intelligence Report's comparison table and the
snapshot / drift / certificate emails render empty or against a lone placeholder
("Sample Supplier Pte Ltd") — the demo shows the frame, not the picture.

This module supplies a believable multi-supplier estate for demo mode only. The
vendor names/websites are baked from `singapore_vendors_bulk_pdpa_test.xlsx`
(repo root, not packaged for ECS) so there's no runtime file/`openpyxl`
dependency and the output is identical in every environment.

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

# (name, website) baked from singapore_vendors_bulk_pdpa_test.xlsx. Source of
# truth is this constant; regenerate from the xlsx if the sample set changes.
_DEMO_SUPPLIERS: List[tuple[str, str]] = [
    ("DBS Bank", "https://www.dbs.com.sg"),
    ("OCBC Bank", "https://www.ocbc.com"),
    ("United Overseas Bank", "https://www.uob.com.sg"),
    ("Singtel", "https://www.singtel.com"),
    ("StarHub", "https://www.starhub.com"),
    ("M1", "https://www.m1.com.sg"),
    ("Singapore Airlines", "https://www.singaporeair.com"),
    ("CapitaLand", "https://www.capitaland.com"),
    ("City Developments Limited", "https://www.cdl.com.sg"),
    ("Keppel Corporation", "https://www.kepcorp.com"),
    ("Sembcorp Industries", "https://www.sembcorp.com"),
    ("SIA Engineering", "https://www.siaec.com.sg"),
    ("ComfortDelGro", "https://www.comfortdelgro.com"),
    ("SMRT Corporation", "https://www.smrt.com.sg"),
    ("Grab", "https://www.grab.com/sg"),
    ("Sea Limited", "https://www.sea.com"),
    ("Shopee Singapore", "https://shopee.sg"),
    ("Lazada Singapore", "https://www.lazada.sg"),
    ("PropertyGuru", "https://www.propertyguru.com.sg"),
    ("Razer", "https://www.razer.com"),
    ("Creative Technology", "https://sg.creative.com"),
    ("ST Engineering", "https://www.stengg.com"),
    ("Wilmar International", "https://www.wilmar-international.com"),
    ("Olam Group", "https://www.olamgroup.com"),
    ("Great Eastern", "https://www.greateasternlife.com"),
    ("NTUC FairPrice", "https://www.fairprice.com.sg"),
    ("Sheng Siong", "https://www.shengsiong.com.sg"),
    ("Jardine Cycle & Carriage", "https://www.jcclgroup.com"),
    ("Mapletree Investments", "https://www.mapletree.com.sg"),
    ("Frasers Property", "https://www.frasersproperty.com"),
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
