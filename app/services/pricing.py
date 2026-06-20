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
    "notarization_addon_1": {
        "name": "Extra Notarization",
        "slug": "notarization_addon_1",
        "price_sgd": 29,
        "price_cents": 2900,
        "description": "Top-up — 1 additional notarization credit when a plan's monthly allowance runs out",
        "type": "one-time",
    },
    "compliance_notarization_10": {
        "name": "Small Batch (Monthly)",
        "slug": "compliance_notarization_10",
        "price_sgd": 390,
        "price_cents": 39000,
        "description": "10 blockchain notarizations per month (SGD 39 each) — batch upload, API access, consolidated certificate",
        "type": "subscription",
        "notarizations_per_month": 10,
    },
    "compliance_notarization_50": {
        "name": "Enterprise Batch (Monthly)",
        "slug": "compliance_notarization_50",
        "price_sgd": 1750,
        "price_cents": 175000,
        "description": "50 blockchain notarizations per month (SGD 35 each) — priority processing, dashboard, webhooks, dedicated support",
        "type": "subscription",
        "notarizations_per_month": 50,
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
        "description": "PDPA Quick Scan + a 7-document PDPA governance pack (DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook, Training, Security Review Log) + RFP Complete + blockchain-anchored Cover Sheet — documents grounded in a live scan of your site (10-doc pack on Amoy testnet)",
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
        "price_sgd": 2990,
        "price_cents": 299000,
        "description": "Annual PDPA compliance monitoring — 2 months free",
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
        "description": "Monthly refresh of your 7-document PDPA governance pack (DPMP, ROPA, Data Inventory, Vendor/DPA Register, Breach Runbook, Training, Security Review Log) plus PDPA + RFP coverage and blockchain-anchored cover sheets",
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
        "name": "Buyer Essentials (Monthly)",
        "slug": "buyer_starter_monthly",
        "price_sgd": 149,
        "price_cents": 14900,
        "description": "Structured vendor due diligence for SMEs and individual procurement officers — L1 Quick Scan on 10 vendors/month, traffic-light dashboard, 1 seat, 1 notarization/month",
        "type": "subscription",
        "notarizations_per_month": 1,
    },
    "buyer_starter_annual": {
        "name": "Buyer Essentials (Annual)",
        "slug": "buyer_starter_annual",
        "price_sgd": 1520,
        "price_cents": 152000,
        "description": "Annual Buyer Essentials — save 15%",
        "type": "subscription",
        "notarizations_per_month": 1,
    },
    "buyer_pro_monthly": {
        "name": "Buyer Professional (Monthly)",
        "slug": "buyer_pro_monthly",
        "price_sgd": 399,
        "price_cents": 39900,
        "description": "Structured procurement teams — 50 Quick + 20 Deep Scans/month, drift tracking, comparison engine, customisable risk weights, 3 seats, webhooks, 5 notarizations/month",
        "type": "subscription",
        "notarizations_per_month": 5,
    },
    "buyer_pro_annual": {
        "name": "Buyer Professional (Annual)",
        "slug": "buyer_pro_annual",
        "price_sgd": 4070,
        "price_cents": 407000,
        "description": "Annual Buyer Professional — save 15%",
        "type": "subscription",
        "notarizations_per_month": 5,
    },
    "buyer_enterprise_monthly": {
        "name": "Buyer Enterprise (Monthly)",
        "slug": "buyer_enterprise_monthly",
        "price_sgd": 799,
        "price_cents": 79900,
        "description": "Audit-ready for enterprise / MAS-regulated procurement — 100 Quick + 100 Deep + 15 Evidence Scans/month, on-chain log, custom frameworks, RBAC, RESTful API, 20 notarizations/month",
        "type": "subscription",
        "notarizations_per_month": 20,
    },
    "buyer_enterprise_annual": {
        "name": "Buyer Enterprise (Annual)",
        "slug": "buyer_enterprise_annual",
        "price_sgd": 8150,
        "price_cents": 815000,
        "description": "Annual Buyer Enterprise — save 15%",
        "type": "subscription",
        "notarizations_per_month": 20,
    },
    "standard_suite_monthly": {
        "name": "Standard Suite (Monthly)",
        "slug": "standard_suite_monthly",
        "price_sgd": 1800,
        "price_cents": 180000,
        "description": "MAS TRM 13 domains with DeepSeek AI gap analysis, RESTful API + webhooks, 50 notarizations/month — built for MAS-regulated banks, fintechs, and healthcare",
        "type": "subscription",
    },
    "pro_suite_monthly": {
        "name": "Pro Suite (Monthly)",
        "slug": "pro_suite_monthly",
        "price_sgd": 4500,
        "price_cents": 450000,
        "description": "Everything in Standard Suite plus SSO (SAML 2.0 + OIDC), white-label reports, and multi-subsidiary management, 100 notarizations/month — built for groups, GLC subsidiaries, and enterprise vendors managing multiple entities",
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
