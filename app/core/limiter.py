from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def _client_ip(request: Request) -> str:
    """Rate-limit key that survives the Cloudflare Tunnel / reverse proxy.

    The API runs behind a Cloudflare Tunnel (no ALB), so `request.client.host`
    is the tunnel's origin IP — identical for every caller. Uvicorn is started
    with `--proxy-headers --forwarded-allow-ips "*"` (see entrypoint.sh), which
    makes it trust `X-Forwarded-For`; we key on the left-most (original client)
    entry so the limit is per-client, not global-per-tunnel-IP. Falls back to
    the socket peer when no forwarded header is present (e.g. local dev).
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        client = xff.split(",")[0].strip()
        if client:
            return client
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, default_limits=["200/minute"])
