from fastapi import APIRouter
from .reports import router as reports_router
from .qr_scan import router as qr_scan_router
from .auth import router as auth_router
from .health import router as health_router
from .consent import router as consent_router
from .stripe_webhook import router as stripe_router
from .stripe_checkout import router as stripe_checkout_router
from .admin import router as admin_router
from .booking import router as booking_router
from .tickets import router as tickets_router
from .verify import router as verify_router
from .bridge import router as bridge_router

# V8 new routes
from .vendor_status import router as vendor_status_router
from .procurement import router as procurement_router
from .rfp_requirements import router as rfp_requirements_router

# V10 new routes
from .tender_check import router as tender_check_router
from .marketplace import router as marketplace_router
from .feature_flags import router as feature_flags_router
from .comparison import router as comparison_router
from .seo import router as seo_router
from .rankings import router as rankings_router
from .funnel import router as funnel_router
from .discovery import router as discovery_router
from .referrals import router as referrals_router
from .widget import router as widget_router
from .sse import router as sse_router
from .notarize import router as notarize_router

# v17 Hardened — Legal consent endpoint
from .legal_consent import router as legal_consent_router

router = APIRouter()

router.include_router(health_router, prefix="/health", tags=["health"])
router.include_router(auth_router, prefix="/auth", tags=["authentication"])
router.include_router(reports_router, prefix="/reports", tags=["reports"])
router.include_router(consent_router, prefix="", tags=["consent"])
router.include_router(stripe_router, prefix="/stripe", tags=["stripe"])
router.include_router(
    stripe_checkout_router, prefix="/stripe", tags=["stripe-checkout"]
)
router.include_router(admin_router, prefix="/admin", tags=["admin"])
router.include_router(booking_router, prefix="/booking", tags=["booking"])
router.include_router(tickets_router, prefix="/tickets", tags=["tickets"])
router.include_router(qr_scan_router, prefix="", tags=["qr-scan"])
router.include_router(verify_router, prefix="/verify", tags=["verify"])
router.include_router(bridge_router, prefix="")
# V8 — Vendor Status, Sector Pressure, CAL Dashboard
router.include_router(vendor_status_router, prefix="/vendor", tags=["vendor-status"])
# V8 — Enterprise Procurement Dashboard
router.include_router(procurement_router, prefix="/procurement", tags=["procurement"])
# V8 — RFP Requirements
router.include_router(
    rfp_requirements_router, prefix="/rfp-requirements", tags=["rfp-requirements"]
)
# V10 — Marketplace & Vendor Directory
router.include_router(marketplace_router, prefix="/marketplace", tags=["marketplace"])
# V10 — Feature Flags
router.include_router(feature_flags_router, prefix="/features", tags=["feature-flags"])
# V10 — Vendor Comparison (Phase 2)
router.include_router(comparison_router, prefix="/compare", tags=["comparison"])
# V10 — SEO Engine (Phase 2)
router.include_router(seo_router, prefix="/seo", tags=["seo"])
# V10 — Rankings & Leaderboard (Phase 3)
router.include_router(rankings_router, prefix="/rankings", tags=["rankings"])
# V10 — Funnel & Analytics
router.include_router(funnel_router, prefix="/funnel", tags=["funnel"])
# V10 — Vendor Discovery (GeBIZ, ACRA)
router.include_router(discovery_router, prefix="/discovery", tags=["discovery"])
# V10 — Referral Program
router.include_router(referrals_router, prefix="/referrals", tags=["referrals"])
# V10 — Embeddable Widget
router.include_router(widget_router, prefix="/widget", tags=["widget"])
# V10 — Server-Sent Events
router.include_router(sse_router, prefix="/sse", tags=["sse"])
# V10 — Notarization Upload
router.include_router(notarize_router, prefix="/notarize", tags=["notarization"])
# v17 Hardened — Legal consent
router.include_router(legal_consent_router, prefix="", tags=["legal-consent"])
# V10 — Tender Win Probability
router.include_router(
    tender_check_router, prefix="/tender-check", tags=["tender-check"]
)
# PDPA Free Scan
from .pdpa_free_scan import router as pdpa_free_scan_router

router.include_router(pdpa_free_scan_router, prefix="/pdpa", tags=["pdpa-free-scan"])
# GeBIZ — Live Tender Feed
from .gebiz import router as gebiz_router

router.include_router(gebiz_router, prefix="/gebiz", tags=["gebiz"])
# Dashboard — real vendor data
from .dashboard import router as dashboard_router

router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
# Dashboard Alerts — consolidated vendor state for alert engine
from .dashboard_alerts import router as dashboard_alerts_router
from .subscription_families import router as subscription_families_router

router.include_router(
    dashboard_alerts_router, prefix="/vendor", tags=["vendor-dashboard-alerts"]
)
router.include_router(subscription_families_router, prefix="/v1", tags=["billing"])
# Resources/Guides CMS
from .resources import router as resources_router

router.include_router(resources_router, prefix="/resources", tags=["resources"])
from .mock_report import router as mock_report_router

router.include_router(mock_report_router, prefix="/mock", tags=["mock"])

from .government import router as government_router

router.include_router(
    government_router, prefix="/government", tags=["government-portal"]
)

# V11 — Compliance Locker
from .compliance_locker import router as compliance_locker_router

router.include_router(
    compliance_locker_router, prefix="/compliance", tags=["compliance-locker"]
)

# V11 — Supply Chain / Managed Vendors
from .managed_vendors import router as managed_vendors_router

router.include_router(
    managed_vendors_router, prefix="/supply-chain", tags=["supply-chain"]
)
