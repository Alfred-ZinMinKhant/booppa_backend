import httpx
from typing import Optional

def get_async_client(
    timeout: float = 15.0,
    max_connections: int = 100,
    max_keepalive_connections: int = 20,
    follow_redirects: bool = True
) -> httpx.AsyncClient:
    """
    Returns a configured httpx.AsyncClient with global timeouts and connection limits.
    Helps prevent runaway tasks or unclosed sockets when external services hang.
    """
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=follow_redirects,
    )

def get_deepseek_client(timeout: float = 60.0) -> httpx.AsyncClient:
    """
    Returns an httpx.AsyncClient specifically tuned for DeepSeek API calls.
    LLM generation can be slow, so the timeout is extended.
    """
    return get_async_client(timeout=timeout)
