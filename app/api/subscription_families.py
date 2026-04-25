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
    families = {
        "pdpa_family": [
            settings.STRIPE_PDPA_MONITOR_MONTHLY,
            settings.STRIPE_PDPA_MONITOR_ANNUAL,
            settings.STRIPE_PDPA_BASIC,
            settings.STRIPE_PDPA_PRO,
            settings.STRIPE_PDPA_QUICK_SCAN,
        ],
        "vendor_family": [
            settings.STRIPE_VENDOR_ACTIVE_MONTHLY,
            settings.STRIPE_VENDOR_ACTIVE_ANNUAL,
            settings.STRIPE_VENDOR_TRUST_PACK,
        ],
        "enterprise_family": [
            settings.STRIPE_ENTERPRISE_MONTHLY,
            settings.STRIPE_ENTERPRISE_PRO_MONTHLY,
            settings.STRIPE_ENTERPRISE_BID_KIT,
            settings.STRIPE_ENTERPRISE_MONTHLY,
        ],
    }

    # Filter out empty/None values to keep response compact
    def compact(lst):
        return [v for v in lst if v]

    return {k: compact(v) for k, v in families.items()}
