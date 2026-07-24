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
    _redis = _redis_lib.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2, max_connections=5)
    _redis.ping()
except Exception as e:
    logger.warning(f"[cache] Redis unavailable, falling back to file cache: {e}")
    _redis = None


def get_redis_client():
    """Return the global Redis client singleton (or None if unavailable)."""
    return _redis

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


def add(key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL) -> bool:
    """Atomically claim `key` only if it does not already exist.

    Returns True if THIS caller won the claim (key was absent and is now set),
    False if the key already existed. Backed by Redis `SET NX EX` so two
    concurrent callers can't both win — use this for once-only guards (e.g.
    "send this email exactly once per subscription") where the get-then-set
    pattern would race.

    Falls back to a best-effort file check when Redis is unreachable; the file
    path is not atomic across processes, but degrades no worse than `get`+`set`.
    """
    if _redis is not None:
        try:
            won = _redis.set(
                f"booppa:{key}", json.dumps(value, ensure_ascii=False), nx=True, ex=ttl
            )
            return bool(won)
        except Exception as e:
            logger.warning(f"[cache] Redis add failed, falling back to file: {e}")

    # File fallback — not cross-process atomic, but preserves once-only intent.
    path = CACHE_DIR / key
    if path.exists():
        return False
    try:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def delete(key: str) -> bool:
    """Drop `key`. Returns True if it existed.

    The release half of `add`'s once-only claim — needed where a claim must be
    deliberately relinquished (e.g. the admin test-checkout "force resend",
    which re-fires activation side effects for the same simulated subscription).
    Production paths should let claims expire by TTL rather than call this.
    """
    existed = False
    if _redis is not None:
        try:
            return bool(_redis.delete(f"booppa:{key}"))
        except Exception as e:
            logger.warning(f"[cache] Redis delete failed, falling back to file: {e}")

    path = CACHE_DIR / key
    try:
        existed = path.exists()
        if existed:
            path.unlink()
    except Exception:
        return False
    return existed


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
