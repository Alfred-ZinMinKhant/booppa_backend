import json
import hashlib
from pathlib import Path
from typing import Any

from app.core.config import settings


CACHE_DIR = Path(settings.MONITOR_CACHE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get(key: str) -> dict | None:
    path = CACHE_DIR / key
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def set(key: str, value: dict[str, Any]) -> None:
    path = CACHE_DIR / key
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
