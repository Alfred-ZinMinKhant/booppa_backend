import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = str(uuid.uuid4())
        token = request_id_ctx.set(req_id)
        
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        
        request_id_ctx.reset(token)
        return response
