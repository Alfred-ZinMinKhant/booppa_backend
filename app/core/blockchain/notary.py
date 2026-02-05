import hashlib
import json
from typing import Any


def notarize(report: dict[str, Any]) -> str:
    payload = json.dumps(report, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
