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
        r = redis.from_url(settings.REDIS_URL)
        r.ping()
        health_status["services"]["redis"] = "healthy"
    except Exception as e:
        health_status["services"]["redis"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"

    return health_status
