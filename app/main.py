from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
try:
    import pkg_resources
    # This helps verify that setuptools is correctly installed in the container
except ImportError:
    pass
from app.core.config import settings
from app.api import router as api_router
from app.core.db import create_tables
from app.core import models as _models
import logging
from mangum import Mangum
from app.api.websocket import socket_app, start_event_relay
import asyncio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="BOOPPA v10.0 Enterprise",
    version="10.0.0",
    description="Auditor-proof evidence generation with blockchain anchoring",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        settings.ALLOWED_ORIGINS.split(",") if settings.ALLOWED_ORIGINS else []
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup"""
    logger.info("Starting BOOPPA v10.0 Enterprise")
    # In production, use Alembic migrations instead of create_tables
    if settings.ENVIRONMENT == "development":
        # ensure models imported so metadata includes all tables
        try:
            _ = _models
        except Exception:
            pass
        create_tables()
        
    # Start WebSocket event relay task
    asyncio.create_task(start_event_relay())


@app.get("/health")
async def health_check():
    """Health check endpoint for load balancers and monitoring"""
    return {"status": "healthy", "version": "10.0.0", "service": "booppa-api"}


# Include API routes.
# NOTE: the second mount below is a DELIBERATE compatibility alias, not an
# accidental duplicate. The Next.js frontend depends on the unversioned /api
# surface for its live polling contracts (GET /api/stripe/checkout/verify,
# GET /api/stripe/rfp/result, POST /api/rfp-intake/{id}/submit). Do not remove
# it without first migrating those callers to /api/v1.
app.include_router(api_router, prefix="/api/v1")
app.include_router(api_router, prefix="/api")

# Mount WebSocket Server
app.mount("/socket.io", socket_app)

# AWS Lambda handler
handler = Mangum(app, lifespan="off")
