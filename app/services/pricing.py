"""
Shared Pricing — Single Source of Truth
========================================
All product SKUs, prices, and Stripe mappings in one place.
Both backend and frontend should reference this.
"""

PRODUCTS = {
    # ── One-time products ───────────────────────────────────────────────────
    "vendor_proof": {
        "name": "Vendor Proof",
        "slug": "vendor_proof",
        "price_sgd": 149,
        "price_cents": 14900,
        "description": "Verification badge + compliance profile",
        "type": "one-time",
    },
    "pdpa_quick_scan": {
        "name": "PDPA Snapshot",
        "slug": "pdpa_quick_scan",
        "price_sgd": 79,
        "price_cents": 7900,
        "description": "PDPA compliance readiness report",
        "type": "one-time",
    },
    "compliance_notarization_1": {
        "name": "Notarization (1 doc)",
        "slug": "compliance_notarization_1",
        "price_sgd": 69,
        "price_cents": 6900,
        "description": "Single-document blockchain notarization",
        "type": "one-time",
    },
    "compliance_notarization_10": {
        "name": "Notarization (10 docs)",
        "slug": "compliance_notarization_10",
        "price_sgd": 390,
        "price_cents": 39000,
        "description": "10-document blockchain notarization pack",
        "type": "one-time",
    },
    "compliance_notarization_50": {
        "name": "Notarization (50 docs)",
        "slug": "compliance_notarization_50",
        "price_sgd": 1750,
        "price_cents": 175000,
        "description": "50-document blockchain notarization pack",
        "type": "one-time",
    },
    "rfp_express": {
        "name": "RFP Express",
        "slug": "rfp_express",
        "price_sgd": 249,
        "price_cents": 24900,
        "description": "Quick RFP response package",
        "type": "one-time",
    },
    "rfp_complete": {
        "name": "RFP Complete",
        "slug": "rfp_complete",
        "price_sgd": 599,
        "price_cents": 59900,
        "description": "Comprehensive RFP evidence package",
        "type": "one-time",
    },
    # ── Bundles (one-time) ──────────────────────────────────────────────────
    "vendor_trust_pack": {
        "name": "Vendor Trust Pack",
        "slug": "vendor_trust_pack",
        "price_sgd": 249,
        "price_cents": 24900,
        "description": "Vendor Proof + PDPA Scan + 2 Notarizations",
        "type": "bundle",
    },
    "rfp_accelerator": {
        "name": "RFP Accelerator",
        "slug": "rfp_accelerator",
        "price_sgd": 449,
        "price_cents": 44900,
        "description": "Vendor Proof + PDPA + 2 Notarizations + RFP Express",
        "type": "bundle",
    },
    "enterprise_bid_kit": {
        "name": "Enterprise Bid Kit",
        "slug": "enterprise_bid_kit",
        "price_sgd": 899,
        "price_cents": 89900,
        "description": "Vendor Proof + PDPA + 7 Notarizations + RFP Complete",
        "type": "bundle",
    },
    # ── Subscriptions ───────────────────────────────────────────────────────
    "vendor_active_monthly": {
        "name": "Vendor Active (Monthly)",
        "slug": "vendor_active_monthly",
        "price_sgd": 39,
        "price_cents": 3900,
        "description": "Monthly profile health checks + score updates",
        "type": "subscription",
    },
    "vendor_active_annual": {
        "name": "Vendor Active (Annual)",
        "slug": "vendor_active_annual",
        "price_sgd": 390,
        "price_cents": 39000,
        "description": "Annual profile health checks + score updates",
        "type": "subscription",
    },
    "pdpa_monitor_monthly": {
        "name": "PDPA Monitor (Monthly)",
        "slug": "pdpa_monitor_monthly",
        "price_sgd": 49,
        "price_cents": 4900,
        "description": "Continuous PDPA compliance monitoring",
        "type": "subscription",
    },
    "pdpa_monitor_annual": {
        "name": "PDPA Monitor (Annual)",
        "slug": "pdpa_monitor_annual",
        "price_sgd": 490,
        "price_cents": 49000,
        "description": "Annual PDPA compliance monitoring",
        "type": "subscription",
    },
    "enterprise_monthly": {
        "name": "Enterprise (Monthly)",
        "slug": "enterprise_monthly",
        "price_sgd": 499,
        "price_cents": 49900,
        "description": "Full enterprise compliance suite",
        "type": "subscription",
    },
    "enterprise_pro_monthly": {
        "name": "Enterprise Pro (Monthly)",
        "slug": "enterprise_pro_monthly",
        "price_sgd": 1499,
        "price_cents": 149900,
        "description": "Enterprise Pro with dedicated support",
        "type": "subscription",
    },
}


def get_product(slug: str) -> dict | None:
    """Get product by slug."""
    return PRODUCTS.get(slug)


def get_all_products() -> list[dict]:
    """Get all products."""
    return list(PRODUCTS.values())
