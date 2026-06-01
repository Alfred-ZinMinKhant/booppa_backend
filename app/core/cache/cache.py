import json
import hashlib
import logging
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

CACHE_DIR = Path(settings.MONITOR_CACHE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Default TTL for cached entries (7 days)
DEFAULT_TTL = 7 * 24 * 3600

# Try to connect to Redis; fall back to file-based cache if unavailable
_redis = None
try:
    import redis as _redis_lib
    _redis = _redis_lib.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    _redis.ping()
except Exception as e:
    logger.warning(f"[cache] Redis unavailable, falling back to file cache: {e}")
    _redis = None


def cache_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get(key: str) -> dict | None:
    if _redis is not None:
        try:
            raw = _redis.get(f"booppa:{key}")
            if raw:
                return json.loads(raw)
            return None
        except Exception as e:
            logger.warning(f"[cache] Redis get failed, trying file: {e}")

    # File fallback
    path = CACHE_DIR / key
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def set(key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL) -> None:
    if _redis is not None:
        try:
            _redis.setex(f"booppa:{key}", ttl, json.dumps(value, ensure_ascii=False))
            return
        except Exception as e:
            logger.warning(f"[cache] Redis set failed, falling back to file: {e}")

    # File fallback
    path = CACHE_DIR / key
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def rate_limit_check(key: str, max_count: int, window_seconds: int) -> bool:
    """
    Atomic per-key rate limit backed by Redis INCR+EXPIRE.

    Returns True if the call is allowed (within budget), False if exceeded.
    The counter resets every `window_seconds`.

    Falls open if Redis is unreachable — this avoids hard-failing the request
    when the cache is offline. Acceptable for free-tier abuse-prevention; if
    abuse is observed in production, replace this with a hard-fail and add
    paging.
    """
    if _redis is None:
        logger.warning("[cache] rate_limit_check called with no Redis — falling open")
        return True
    try:
        bucket = f"booppa:ratelimit:{key}"
        count = _redis.incr(bucket)
        if count == 1:
            # First hit in the window — set TTL so the bucket auto-resets.
            _redis.expire(bucket, window_seconds)
        return count <= max_count
    except Exception as e:
        logger.warning(f"[cache] rate_limit_check Redis error, falling open: {e}")
        return True
