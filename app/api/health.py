from fastapi import APIRouter
from app.core.db import SessionLocal
from sqlalchemy import text
import redis
from app.core.config import settings

router = APIRouter()

@router.get("")
async def health_check():
    """Comprehensive health check"""
    health_status = {
        "status": "healthy",
        "version": "10.0.0",
        "services": {}
    }

    # Check database
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
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

    return health_status
