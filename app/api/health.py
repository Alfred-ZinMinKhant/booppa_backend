from app.core.route_classes import RetryAPIRoute
from fastapi import APIRouter
from app.core.db import SessionLocal
from sqlalchemy import text
import redis
from app.core.config import settings

router = APIRouter(route_class=RetryAPIRoute)

@router.get("")
async def health_check():
    """Comprehensive health check"""
    health_status = {
        "status": "healthy",
        "version": "10.0.0",
        "services": {}
    }

    # Check database.
    #
    # `SELECT 1` proves only that a connection opens. On 2026-08-08 a deploy
    # shipped mapped columns three migrations ahead of the DB, so every ORM
    # query against `users` raised UndefinedColumn — login, registration,
    # checkout and webhook fulfilment were all dead — and this endpoint
    # returned 200 for the whole outage because `SELECT 1` needs no schema.
    # Querying through the ORM exercises the mapping the application actually
    # depends on, which is the thing that breaks (AUDIT_2026-08-08.md P0-C).
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            from app.core.models import User

            db.query(User).limit(1).first()
        finally:
            db.close()
        health_status["services"]["database"] = "healthy"
    except Exception as e:
        health_status["services"]["database"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"

    # Check Redis
    try:
        from app.core.cache.cache import get_redis_client
        r = get_redis_client()
        if r:
            r.ping()
            health_status["services"]["redis"] = "healthy"
        else:
            health_status["services"]["redis"] = "unhealthy: no connection pool"
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["services"]["redis"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"

    # Payments. Reported as a fact, not a pass/fail — test mode is a legitimate
    # state and must not mark the service degraded. But it has to be *visible*:
    # the only reason production ran on test keys unnoticed is that no endpoint
    # or log ever said which mode it was in (AUDIT_2026-08-08.md P0-D).
    #
    # No key material is exposed — only the mode, the count, and any
    # inconsistency between the three places Stripe credentials live.
    try:
        from app.billing.stripe_mode import (
            check_consistency,
            price_ids_for_audit,
            secret_key_mode,
        )

        problems = check_consistency()
        health_status["stripe"] = {
            "mode": secret_key_mode(),
            "prices_configured": len(price_ids_for_audit()),
            "config_problems": problems,
        }
        # A half-migrated Stripe config IS degraded: it takes money and fails to
        # deliver, which is worse than being cleanly offline.
        if problems:
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["stripe"] = {"mode": "unknown", "error": str(e)}

    return health_status
