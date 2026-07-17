from fastapi import Depends, FastAPI, HTTPException, Request
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
from app.core.json_logger import setup_json_logging
import asyncio
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.middleware import RequestIDMiddleware

setup_json_logging(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

from app.core.limiter import limiter

# Sentry error tracking — inert unless SENTRY_DSN is set, so this is a no-op in
# environments (incl. local/CI) that don't configure it.
if settings.SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        )
        logger.info("Sentry error tracking initialised")
    except Exception as e:  # pragma: no cover - defensive; never block boot
        logger.warning(f"Sentry init skipped: {e}")


def verify_metrics_token(request: Request) -> None:
    """Gate the Prometheus /metrics endpoint.

    Closed by default: when METRICS_TOKEN is unset, /metrics returns 404 so the
    metric surface is never exposed unauthenticated behind the public tunnel.
    When set, the caller must supply the token via `Authorization: Bearer <t>`
    or `?token=<t>`.
    """
    expected = settings.METRICS_TOKEN
    if not expected:
        raise HTTPException(status_code=404)
    auth = request.headers.get("authorization", "")
    presented = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
    if presented != expected:
        raise HTTPException(status_code=404)

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
app.add_middleware(RequestIDMiddleware)

Instrumentator().instrument(app).expose(app, dependencies=[Depends(verify_metrics_token)])

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
        
    # Start WebSocket event relay task (tracked so shutdown can cancel it)
    app.state.relay_task = asyncio.create_task(start_event_relay())


@app.on_event("shutdown")
async def shutdown_event():
    """Drain in-flight work cleanly on ECS task replacement.

    Cancels the tracked WebSocket relay task and disposes the SQLAlchemy engine
    so pooled connections are returned rather than severed mid-request.
    """
    logger.info("Shutting down BOOPPA v10.0 Enterprise")
    relay_task = getattr(app.state, "relay_task", None)
    if relay_task and not relay_task.done():
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Relay task shutdown error: {e}")
    try:
        from app.core.db import engine
        engine.dispose()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Engine dispose error: {e}")


@app.get("/health")
async def health_check():
    """Liveness probe — cheap, static 200. Answers 'is the process up?' only."""
    return {"status": "healthy", "version": "10.0.0", "service": "booppa-api"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe — verifies the app can actually serve: DB + Redis reachable.

    Returns 503 (not a static 200) so the ECS container health check and
    `aws ecs wait services-stable` gate on real serving readiness; a
    boot-broken image (bad DB creds, unreachable Redis) fails the rollout
    instead of deploying 'green'.
    """
    checks: dict[str, str] = {}
    ok = True

    try:
        from app.core.db import SessionLocal
        from sqlalchemy import text

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"
        ok = False

    try:
        from app.core.cache.cache import get_redis_client

        r = get_redis_client()
        if r:
            r.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "error: no connection pool"
            ok = False
    except Exception as e:
        checks["redis"] = f"error: {e}"
        ok = False

    body = {"status": "ready" if ok else "not_ready", "checks": checks}
    if not ok:
        return JSONResponse(status_code=503, content=body)
    return body


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
