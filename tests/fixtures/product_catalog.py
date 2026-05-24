"""Canonical SKU catalog for parametrized tests.

Mirrors the authoritative sources:
  - app/api/stripe_checkout.py:MODE_MAP — what /checkout accepts
  - app/api/stripe_webhook.py:{SUBSCRIPTION_PRODUCT_TYPES, BUNDLE_COMPONENTS,
    RFP_PRODUCT_TYPES, NOTARIZATION_CREDIT_AMOUNTS} — what /webhook routes

If a SKU appears in MODE_MAP it MUST appear here. Drift between MODE_MAP and
this file means the test suite is stale — fix the catalog, not the test.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProductCase:
    product_type: str
    mode: str                          # "payment" | "subscription"
    family: str                        # "one_time" | "bundle" | "subscription"
    expected_fulfillment: str          # symbolic name of the handler we expect to hit
    expected_pdf: bool = False
    pdf_framework: str | None = None   # value passed to PDFService.generate_pdf
    expects_email: bool = True
    email_subject_keywords: tuple[str, ...] = field(default_factory=tuple)
    # Optional metadata the webhook needs in order to fulfill; tests merge this
    # into the session metadata
    required_metadata: dict[str, str] = field(default_factory=dict)


ONE_TIME: list[ProductCase] = [
    ProductCase(
        product_type="vendor_proof",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_vendor_proof",
        expected_pdf=False,
        email_subject_keywords=("Vendor Proof",),
    ),
    ProductCase(
        product_type="pdpa_quick_scan",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_pdpa",
        expected_pdf=True,
        pdf_framework="pdpa_quick_scan",
        email_subject_keywords=("PDPA",),
    ),
    ProductCase(
        product_type="rfp_express",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_rfp_package",
        expected_pdf=True,
        pdf_framework="rfp_express",
        email_subject_keywords=("RFP",),
        required_metadata={"rfp_description": "Need cloud migration vendor for SG retail chain."},
    ),
    ProductCase(
        product_type="rfp_complete",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_rfp_package",
        expected_pdf=True,
        pdf_framework="rfp_complete",
        email_subject_keywords=("RFP",),
        required_metadata={"rfp_description": "Need cloud migration vendor for SG retail chain."},
    ),
    ProductCase(
        product_type="compliance_notarization_1",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_notarization",
        expected_pdf=False,  # cert is generated on redemption, not at checkout
        email_subject_keywords=("Notarization",),
    ),
    ProductCase(
        product_type="compliance_notarization_10",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_notarization",
        email_subject_keywords=("Notarization",),
    ),
    ProductCase(
        product_type="compliance_notarization_50",
        mode="payment",
        family="one_time",
        expected_fulfillment="_fulfill_notarization",
        email_subject_keywords=("Notarization",),
    ),
]

BUNDLES: list[ProductCase] = [
    ProductCase(
        product_type="vendor_trust_pack",
        mode="payment",
        family="bundle",
        expected_fulfillment="_fulfill_bundle",
        email_subject_keywords=("Trust",),
    ),
    ProductCase(
        product_type="rfp_accelerator",
        mode="payment",
        family="bundle",
        expected_fulfillment="_fulfill_bundle",
        email_subject_keywords=("RFP", "Accelerator"),
    ),
    ProductCase(
        product_type="enterprise_bid_kit",
        mode="payment",
        family="bundle",
        expected_fulfillment="_fulfill_bundle",
        email_subject_keywords=("Enterprise",),
    ),
    ProductCase(
        product_type="compliance_evidence_pack",
        mode="payment",
        family="bundle",
        expected_fulfillment="_fulfill_bundle",
        email_subject_keywords=("Compliance", "Evidence"),
    ),
]

SUBSCRIPTIONS: list[ProductCase] = [
    ProductCase(p, "subscription", "subscription", "_activate_subscription",
                expects_email=True, email_subject_keywords=("subscription",))
    for p in [
        "vendor_active_monthly", "vendor_active_annual",
        "pdpa_monitor_monthly", "pdpa_monitor_annual",
        "enterprise_monthly", "enterprise_pro_monthly",
        "standard_suite_monthly", "pro_suite_monthly",
        "evaluate_suppliers_monthly", "verify_supplier_evidence_monthly",
        "compliance_evidence_monthly",
        "tender_intelligence_monthly", "tender_intelligence_annual",
        "vendor_pro_monthly", "vendor_pro_annual",
    ]
]

ALL_SKUS: list[ProductCase] = ONE_TIME + BUNDLES + SUBSCRIPTIONS


def sku_id(case: ProductCase) -> str:
    """pytest parametrize id helper."""
    return case.product_type
