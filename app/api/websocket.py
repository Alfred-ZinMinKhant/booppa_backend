import asyncio
import socketio
from typing import Dict, Set

from app.core.auth import verify_access_token

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=True,
    engineio_logger=False
)

socket_app = socketio.ASGIApp(sio)

# Mapping of vendor_id to set of socket session IDs
vendor_sockets: Dict[str, Set[str]] = {}

@sio.event
async def connect(sid, environ, auth):
    token = auth.get('token') if auth else None
    if not token:
        return False
        
    try:
        if token.startswith("Bearer "):
            token = token.split(" ")[1]
            
        payload = verify_access_token(token)
        if not payload:
            return False
            
        # We assume subject is email, we need to associate this with vendor_id
        # For simplicity, if frontend uses sub as ID or email, we map it
        # Actually in app/api/bridge.py we returned full user in /auth/login, 
        # Here we just use the token's sub (email or ID) as room identifier
        vendor_identifier = payload.get('sub')
        
        if vendor_identifier not in vendor_sockets:
            vendor_sockets[vendor_identifier] = set()
        vendor_sockets[vendor_identifier].add(sid)
        
        await sio.save_session(sid, {'vendor_id': vendor_identifier})
        print(f"[WS] Vendor {vendor_identifier} connected (sid={sid})")
        return True
    except Exception as e:
        print(f"[WS] Connection rejected: {e}")
        return False

@sio.event
async def disconnect(sid):
    try:
        session = await sio.get_session(sid)
        vendor_id = session.get('vendor_id')
        if vendor_id and vendor_id in vendor_sockets:
            vendor_sockets[vendor_id].discard(sid)
            if not vendor_sockets[vendor_id]:
                del vendor_sockets[vendor_id]
        print(f"[WS] Disconnected sid={sid}")
    except Exception:
        pass

async def emit_to_vendor(vendor_identifier: str, event: str, data: dict):
    if vendor_identifier in vendor_sockets:
        for sid in vendor_sockets[vendor_identifier]:
            await sio.emit(event, data, room=sid)

async def start_event_relay():
    """
    Background task to relay events from Redis or internal event bus
    to WebSockets in production.
    For this implementation, direct function calls from the API
    endpoints (`emit_to_vendor`) will be used to broadcast real-time data.
    """
    pass
