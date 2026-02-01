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
