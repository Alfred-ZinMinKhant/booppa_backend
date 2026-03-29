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
