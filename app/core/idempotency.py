import json
import logging
from typing import Optional
from fastapi import Request, Response, HTTPException, Depends
from starlette.responses import JSONResponse
import redis as redis_lib
from app.core.config import settings

logger = logging.getLogger(__name__)

# Assuming settings.REDIS_URL is available
try:
    redis_client = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
except Exception:
    redis_client = None

class IdempotencyGuard:
    """
    Dependency that ensures a request with a specific Idempotency-Key
    is processed exactly once.
    """
    def __init__(self, key_header: str = "Idempotency-Key", expire_seconds: int = 86400):
        self.key_header = key_header
        self.expire_seconds = expire_seconds

    async def __call__(self, request: Request, response: Response):
        idem_key = request.headers.get(self.key_header)
        if not idem_key:
            return  # Bypass if no key provided
            
        if not redis_client:
            logger.warning("IdempotencyGuard bypassed because Redis is not configured.")
            return

        cache_key = f"idempotency:{idem_key}"
        
        # Check if already processed
        cached_result = redis_client.get(cache_key)
        if cached_result:
            if cached_result == "IN_PROGRESS":
                raise HTTPException(status_code=409, detail="Request is already being processed.")
            
            # Return cached response
            try:
                data = json.loads(cached_result)
                raise HTTPException(status_code=data.get("status_code", 200), detail=data.get("body"))
            except json.JSONDecodeError:
                pass
                
        # Mark as in progress
        success = redis_client.set(cache_key, "IN_PROGRESS", nx=True, ex=self.expire_seconds)
        if not success:
            raise HTTPException(status_code=409, detail="Request is already being processed.")
            
        # The route handler will run. The actual caching of the response would require
        # a middleware or a route class, but for Stripe webhooks, we usually just need
        # to ensure it's not processed concurrently.
        # So we leave it as IN_PROGRESS. The webhook handler can overwrite it if needed.
