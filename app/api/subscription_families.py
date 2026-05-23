from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()


@router.get("/subscription-families")
def get_subscription_families():
    """Return configured Stripe price ids grouped into logical families.

    Frontend should call `/api/v1/subscription-families` and use the price ids
    (or canonical product_type names) to detect active families instead of
    substring matching.
    """
    def _opt(name: str):
        return getattr(settings, name, None)

    families = {
        "pdpa_family": [
            _opt("STRIPE_PDPA_MONITOR_MONTHLY"),
            _opt("STRIPE_PDPA_MONITOR_ANNUAL"),
            _opt("STRIPE_PDPA_QUICK_SCAN"),
        ],
        "vendor_family": [
            _opt("STRIPE_VENDOR_ACTIVE_MONTHLY"),
            _opt("STRIPE_VENDOR_ACTIVE_ANNUAL"),
            _opt("STRIPE_VENDOR_PRO_MONTHLY"),
            _opt("STRIPE_VENDOR_PRO_ANNUAL"),
            _opt("STRIPE_VENDOR_TRUST_PACK"),
        ],
        "enterprise_family": [
            _opt("STRIPE_ENTERPRISE_MONTHLY"),
            _opt("STRIPE_ENTERPRISE_PRO_MONTHLY"),
            _opt("STRIPE_ENTERPRISE_BID_KIT"),
        ],
        "buyer_family": [
            _opt("STRIPE_EVALUATE_SUPPLIERS_MONTHLY"),
            _opt("STRIPE_VERIFY_SUPPLIER_EVIDENCE_MONTHLY"),
            _opt("STRIPE_ENTERPRISE_MONTHLY"),
        ],
        "compliance_family": [
            _opt("STRIPE_COMPLIANCE_EVIDENCE_MONTHLY"),
            _opt("STRIPE_COMPLIANCE_EVIDENCE_PACK"),
        ],
        "suite_family": [
            _opt("STRIPE_STANDARD_SUITE_MONTHLY"),
            _opt("STRIPE_PRO_SUITE_MONTHLY"),
        ],
        "tender_intelligence_family": [
            _opt("STRIPE_TENDER_INTELLIGENCE_MONTHLY"),
            _opt("STRIPE_TENDER_INTELLIGENCE_ANNUAL"),
        ],
    }

    # Filter out empty/None values to keep response compact
    def compact(lst):
        return [v for v in lst if v]

    return {k: compact(v) for k, v in families.items()}
