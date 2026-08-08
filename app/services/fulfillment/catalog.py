"""Canonical product-type catalogue for the purchase → fulfillment pipeline.

This module is the single source of truth for "which SKUs exist and what is in
them". It deliberately imports nothing from `app` so every other module can
import it without a cycle.

**Why this exists.** `RFP_PRODUCT_TYPES` and `BUNDLE_COMPONENTS` were previously
copy-pasted into five modules (`stripe_webhook.py` plus four under
`app/services/fulfillment/`). They happened to be identical, but nothing enforced
that, and a closely-related drift shipped: `stripe_checkout.py:checkout_verify`
listed five brief-bearing SKUs in one place and only two in its recovery branch,
so a missing intake row was never recovered for `rfp_accelerator`,
`enterprise_bid_kit` or `compliance_evidence_pack` — the three most expensive
SKUs stranded the buyer on the success page after payment.

Add a SKU here and nowhere else.
"""

# Standalone RFP kits. These are the only values `PendingRfpIntake.rfp_product_type`
# may hold — a bundle name is NOT a kit type (see `rfp_component_for`).
RFP_PRODUCT_TYPES = {"rfp_express", "rfp_complete"}

# Bundles fan out to component fulfillment. `rfp` names the kit the bundle
# includes, or None when it carries no RFP component.
BUNDLE_COMPONENTS = {
    "vendor_trust_pack": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": None,
    },
    "rfp_accelerator": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": "rfp_express",
    },
    "enterprise_bid_kit": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 7,  # 2 from Trust Pack + 5 additional
        "rfp": "rfp_complete",
    },
    "compliance_evidence_pack": {
        "vendor_proof": False,
        "pdpa": True,
        "notarization_count": 1,
        "rfp": "rfp_complete",
        "cover_sheet": True,  # auto-fires once PDPA + RFP + BCEP pack are all ready
    },
}


def rfp_component_for(product_type: str | None) -> str | None:
    """The RFP kit type a purchase defers to, or None if it defers no brief.

    Standalone kits map to themselves; bundles map to their `rfp` component.
    Use this to populate `PendingRfpIntake.rfp_product_type` — writing the
    bundle name there produces a row `fulfill_rfp_task` cannot fulfil.
    `bundle_source` is where the bundle name belongs.
    """
    if not product_type:
        return None
    if product_type in RFP_PRODUCT_TYPES:
        return product_type
    return (BUNDLE_COMPONENTS.get(product_type) or {}).get("rfp")


# Every SKU whose buyer owes us a brief before the kit can be generated. Derived,
# never hand-listed, so it cannot fall out of step with BUNDLE_COMPONENTS.
RFP_BRIEF_PRODUCT_TYPES = RFP_PRODUCT_TYPES | {
    name for name, parts in BUNDLE_COMPONENTS.items() if parts.get("rfp")
}
