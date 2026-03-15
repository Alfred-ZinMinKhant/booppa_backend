"""
Shared Pricing — Single Source of Truth
========================================
All product SKUs, prices, and Stripe mappings in one place.
Both backend and frontend should reference this.
"""

PRODUCTS = {
    "vendor-proof": {
        "name": "Vendor Proof",
        "slug": "vendor-proof",
        "price_sgd": 149,
        "price_cents": 14900,
        "description": "Verification badge + compliance profile",
        "type": "one-time",
    },
    "pdpa-snapshot": {
        "name": "PDPA Snapshot",
        "slug": "pdpa-snapshot",
        "price_sgd": 79,
        "price_cents": 7900,
        "description": "PDPA compliance readiness report",
        "type": "one-time",
    },
    "notarization": {
        "name": "Notarization",
        "slug": "notarization",
        "price_sgd": 69,
        "price_cents": 6900,
        "description": "Single-document blockchain notarization",
        "type": "one-time",
    },
    "rfp-express": {
        "name": "RFP Express",
        "slug": "rfp-express",
        "price_sgd": 249,
        "price_cents": 24900,
        "description": "Quick RFP response package",
        "type": "one-time",
    },
    "rfp-complete": {
        "name": "RFP Complete",
        "slug": "rfp-complete",
        "price_sgd": 599,
        "price_cents": 59900,
        "description": "Comprehensive RFP evidence package",
        "type": "one-time",
    },
}


def get_product(slug: str) -> dict | None:
    """Get product by slug."""
    return PRODUCTS.get(slug)


def get_all_products() -> list[dict]:
    """Get all products."""
    return list(PRODUCTS.values())
