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
        "price_sgd": 299,
        "price_cents": 29900,
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
    "compliance_evidence_pack": {
        "name": "Compliance Bundle",
        "slug": "compliance_evidence_pack",
        "price_sgd": 799,
        "price_cents": 79900,
        "description": "PDPA Quick Scan + RFP Complete + Compliance Cover Sheet (3-doc pack on Amoy testnet)",
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
    "vendor_pro_monthly": {
        "name": "Vendor Pro (Monthly)",
        "slug": "vendor_pro_monthly",
        "price_sgd": 99,
        "price_cents": 9900,
        "description": "Compliance visibility and tender intelligence for growing vendors — Vendor Active + quarterly PDPA scan + 1 notarization/mo + tender analytics + competitor awareness",
        "type": "subscription",
    },
    "vendor_pro_annual": {
        "name": "Vendor Pro (Annual)",
        "slug": "vendor_pro_annual",
        "price_sgd": 1099,
        "price_cents": 109900,
        "description": "Annual Vendor Pro — 2 months free vs monthly",
        "type": "subscription",
    },
    "pdpa_monitor_monthly": {
        "name": "PDPA Monitor (Monthly)",
        "slug": "pdpa_monitor_monthly",
        "price_sgd": 299,
        "price_cents": 29900,
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
    "compliance_evidence_monthly": {
        "name": "Compliance Evidence (Monthly)",
        "slug": "compliance_evidence_monthly",
        "price_sgd": 499,
        "price_cents": 49900,
        "description": "All-in-one PDF/Docs evidence with PDPA + RFP coverage and blockchain-anchored cover sheets",
        "type": "subscription",
    },
    "evaluate_suppliers_monthly": {
        "name": "Evaluate Suppliers (Monthly)",
        "slug": "evaluate_suppliers_monthly",
        "price_sgd": 499,
        "price_cents": 49900,
        "description": "Enhanced vendor due diligence with risk scoring, drift tracking, team collaboration",
        "type": "subscription",
    },
    "verify_supplier_evidence_monthly": {
        "name": "Verify Supplier Evidence (Monthly)",
        "slug": "verify_supplier_evidence_monthly",
        "price_sgd": 799,
        "price_cents": 79900,
        "description": "Full audit-ready verification suite with on-chain logs and custom evaluation frameworks",
        "type": "subscription",
    },
    # ── Buyer subscriptions (new ladder — replaces evaluate_suppliers /
    #    verify_supplier_evidence above; those entries remain only for
    #    backward compatibility with existing Stripe subscriptions until
    #    they are migrated). ────────────────────────────────────────────
    "buyer_starter_monthly": {
        "name": "Buyer Starter (Monthly)",
        "slug": "buyer_starter_monthly",
        "price_sgd": 99,
        "price_cents": 9900,
        "description": "Entry-level vendor evaluation — 10 vendor scans/month, basic risk signals, single-user",
        "type": "subscription",
    },
    "buyer_starter_annual": {
        "name": "Buyer Starter (Annual)",
        "slug": "buyer_starter_annual",
        "price_sgd": 990,
        "price_cents": 99000,
        "description": "Annual Buyer Starter — 2 months free",
        "type": "subscription",
    },
    "buyer_pro_monthly": {
        "name": "Buyer Pro (Monthly)",
        "slug": "buyer_pro_monthly",
        "price_sgd": 399,
        "price_cents": 39900,
        "description": "Active buyer teams — 50 vendor scans/month, comparison engine, drift tracking, 5 seats",
        "type": "subscription",
    },
    "buyer_pro_annual": {
        "name": "Buyer Pro (Annual)",
        "slug": "buyer_pro_annual",
        "price_sgd": 3990,
        "price_cents": 399000,
        "description": "Annual Buyer Pro — 2 months free",
        "type": "subscription",
    },
    "buyer_enterprise_monthly": {
        "name": "Buyer Enterprise (Monthly)",
        "slug": "buyer_enterprise_monthly",
        "price_sgd": 999,
        "price_cents": 99900,
        "description": "Institutional procurement — 250 vendor scans/month + unlimited re-runs, on-chain logs, SSO, audit-ready exports",
        "type": "subscription",
    },
    "buyer_enterprise_annual": {
        "name": "Buyer Enterprise (Annual)",
        "slug": "buyer_enterprise_annual",
        "price_sgd": 9990,
        "price_cents": 999000,
        "description": "Annual Buyer Enterprise — 2 months free",
        "type": "subscription",
    },
    "notana_document_monthly": {
        "name": "Notana Document (Add-On, Monthly)",
        "slug": "notana_document_monthly",
        "price_sgd": 199,
        "price_cents": 19900,
        "description": "Notarisation add-on for buyer plans — 10 buyer-initiated notarisations/month with on-chain timestamped evidence",
        "type": "subscription",
    },
    "standard_suite_monthly": {
        "name": "Standard Suite (Monthly)",
        "slug": "standard_suite_monthly",
        "price_sgd": 1800,
        "price_cents": 180000,
        "description": "MAS TRM 13 domains, AI gap analysis, 5,000 notarizations/month, RESTful API + webhooks",
        "type": "subscription",
    },
    "pro_suite_monthly": {
        "name": "Pro Suite (Monthly)",
        "slug": "pro_suite_monthly",
        "price_sgd": 4500,
        "price_cents": 450000,
        "description": "Everything in Standard Suite plus SSO, white-label, multi-subsidiary, unlimited notarizations",
        "type": "subscription",
    },
    "tender_intelligence_monthly": {
        "name": "Tender Intelligence (Monthly)",
        "slug": "tender_intelligence_monthly",
        "price_sgd": 149,
        "price_cents": 14900,
        "description": "Sector trend reports, historical award lookups, AI-driven bid/watch/pass timing, supplier benchmarking, monthly digest",
        "type": "subscription",
    },
    "tender_intelligence_annual": {
        "name": "Tender Intelligence (Annual)",
        "slug": "tender_intelligence_annual",
        "price_sgd": 1499,
        "price_cents": 149900,
        "description": "Annual Tender Intelligence — effectively two months free vs monthly billing",
        "type": "subscription",
    },
}


def get_product(slug: str) -> dict | None:
    """Get product by slug."""
    return PRODUCTS.get(slug)


def get_all_products() -> list[dict]:
    """Get all products."""
    return list(PRODUCTS.values())
