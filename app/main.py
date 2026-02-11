from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BOOPPA v10.0 Enterprise",
    version="10.0.0",
    description="Auditor-proof evidence generation with blockchain anchoring",
    docs_url="/docs",
    redoc_url="/redoc",
)

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


@app.get("/health")
async def health_check():
    """Health check endpoint for load balancers and monitoring"""
    return {"status": "healthy", "version": "10.0.0", "service": "booppa-api"}


# Include API routes
app.include_router(api_router, prefix="/api/v1")
# Also expose the same API surface at /api for compatibility with frontend
# callers that expect unversioned endpoints (e.g. /api/stripe/checkout).
app.include_router(api_router, prefix="/api")
