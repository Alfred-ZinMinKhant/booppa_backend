"""
SSE (Server-Sent Events) Routes
================================
Lightweight one-way real-time push updates for frontend.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.core.db import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

# Simple in-memory event queue (use Redis pub/sub for multi-process)
_event_subscribers: list = []


def publish_event(event_type: str, data: dict):
    """Publish an event to all SSE subscribers."""
    message = {"type": event_type, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()}
    for queue in _event_subscribers:
        try:
            queue.put_nowait(message)
        except Exception as exc:
            logger.warning("[SSE] Failed to deliver event to subscriber queue: %s", exc)


async def _event_generator(request: Request, queue: asyncio.Queue):
    """Generate SSE events."""
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
            except asyncio.TimeoutError:
                yield f": keepalive {datetime.now(timezone.utc).isoformat()}\n\n"
    finally:
        if queue in _event_subscribers:
            _event_subscribers.remove(queue)


@router.get("/events")
async def sse_events(request: Request):
    """Subscribe to real-time events via Server-Sent Events."""
    queue = asyncio.Queue(maxsize=100)
    _event_subscribers.append(queue)

    return StreamingResponse(
        _event_generator(request, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/publish")
async def publish(event_type: str, data: dict = {}):
    """Publish an event (admin/internal)."""
    publish_event(event_type, data)
    return {"published": True, "subscribers": len(_event_subscribers)}
