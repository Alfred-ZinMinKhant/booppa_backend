"""Fulfillment Service — public API for product provisioning & delivery.

This module is the canonical import path for all fulfillment logic used by
background workers, admin endpoints, and internal orchestration.

Currently it re-exports functions that still live inside
`app.api.stripe_webhook` (incremental migration). New fulfillment logic
should be added here directly, and existing functions will be migrated
into this module over time.

WHY THIS EXISTS:
Background workers (`tasks.py`, `csp_tasks.py`) and admin endpoints were
importing directly from the webhook controller, creating a tight coupling
between payment infrastructure and business logic. This facade decouples
the callers from the webhook module so the webhook can eventually be
reduced to pure event-parsing + delegation.
"""

# ── Re-exports from stripe_webhook (incremental migration) ──────────────────
# These will eventually be moved here as standalone functions/classes.
# Until then, this module serves as the single import point.

from app.api.stripe_webhook import (  # noqa: F401
    # Constants
    SUBSCRIPTION_PRODUCT_TYPES,
    BUNDLE_COMPONENTS,
    RFP_PRODUCT_TYPES,
    NOTARIZATION_PRODUCT_TYPES,
    PDPA_PRODUCT_TYPES,
    VENDOR_PROOF_PRODUCT_TYPES,
    CSP_ONETIME_PRODUCT_TYPES,
    NOTARIZATION_CREDIT_AMOUNTS,
    # Subscription activation
    _activate_subscription as activate_subscription,
    # Fulfillment functions
    _fulfill_pdpa as fulfill_pdpa,
    _fulfill_vendor_proof as fulfill_vendor_proof,
    _fulfill_notarization as fulfill_notarization,
    _fulfill_rfp_package as fulfill_rfp_package,
    _fulfill_bundle as fulfill_bundle,
    _fulfill_standalone_no_report as fulfill_standalone_no_report,
    _fulfill_compliance_evidence_pack as fulfill_compliance_evidence_pack,
    _defer_rfp_to_intake as defer_rfp_to_intake,
    # Helpers
    _maybe_fire_cover_sheet as maybe_fire_cover_sheet,
    _fire_strategy_6 as fire_strategy_6,
    _alert_payment_fulfillment_issue as alert_payment_fulfillment_issue,
    _create_stub_report as create_stub_report,
)
