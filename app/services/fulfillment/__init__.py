"""Fulfillment Service — public API for product provisioning & delivery.

This module is the canonical import path for all fulfillment logic used by
background workers, admin endpoints, and internal orchestration.

"""

from app.services.fulfillment.subscriptions import (
    _activate_subscription as activate_subscription,
    _csp_activation_email_html as csp_activation_email_html,
)

from app.services.fulfillment.single_products import (
    _fulfill_pdpa as fulfill_pdpa,
    _fulfill_vendor_proof as fulfill_vendor_proof,
    _fulfill_notarization as fulfill_notarization,
    _fulfill_rfp_package as fulfill_rfp_package,
    _defer_rfp_to_intake as defer_rfp_to_intake,
)

from app.services.fulfillment.bundles import (
    _fulfill_bundle as fulfill_bundle,
    _fulfill_standalone_no_report as fulfill_standalone_no_report,
    _fulfill_compliance_evidence_pack as fulfill_compliance_evidence_pack,
)

from app.services.fulfillment.helpers import (
    _maybe_fire_cover_sheet as maybe_fire_cover_sheet,
    _fire_strategy_6 as fire_strategy_6,
    _alert_payment_fulfillment_issue as alert_payment_fulfillment_issue,
    _revert_subscription_score_lever as revert_subscription_score_lever,
    _create_stub_report as create_stub_report,
)

# Constants imported from the new files (since they were duplicated, we can import from any, e.g. helpers)
from app.services.fulfillment.helpers import (
    SUBSCRIPTION_PRODUCT_TYPES,
    BUNDLE_COMPONENTS,
    RFP_PRODUCT_TYPES,
    NOTARIZATION_PRODUCT_TYPES,
    PDPA_PRODUCT_TYPES,
    VENDOR_PROOF_PRODUCT_TYPES,
    CSP_ONETIME_PRODUCT_TYPES,
    NOTARIZATION_CREDIT_AMOUNTS,
)
