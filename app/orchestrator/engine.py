import asyncio
import logging
from typing import Any

from app.core.cache.cache import get, set, cache_key
from app.core.blockchain.notary import notarize
from app.integrations.scan1.adapter import run_scan_async
from app.integrations.ai.adapter import ai_preview, ai_full
from app.core.config import settings
from app.services.blockchain import BlockchainService


logger = logging.getLogger(__name__)


def _resolve_thresholds() -> dict[str, int]:
    thresholds = settings.MONITOR_RISK_THRESHOLDS or {}
    return {
        "LOW": int(thresholds.get("LOW", 30)),
        "MEDIUM": int(thresholds.get("MEDIUM", 60)),
        "HIGH": int(thresholds.get("HIGH", 100)),
    }


async def run(url: str) -> dict[str, Any]:
    key = cache_key(url)
    cached = get(key)
    if cached:
        return cached

    scan = await run_scan_async(url)
    scan_payload = scan.model_dump()

    thresholds = _resolve_thresholds()
    risk = scan_payload.get("overall_risk_score", 0)

    ai_result: dict[str, Any] | None
    if risk < thresholds["LOW"]:
        ai_result = None
    elif risk < thresholds["MEDIUM"]:
        ai_result = await ai_preview(scan_payload)
    else:
        ai_result = await ai_full(scan_payload)

    report: dict[str, Any] = {
        "url": url,
        "scan": scan_payload,
        "ai": ai_result,
    }
    report["notary_hash"] = notarize(report)

    tx_hash = None
    if settings.MONITOR_ANCHOR_ENABLED:
        try:
            blockchain = BlockchainService()
            tx_hash = await blockchain.anchor_evidence(report["notary_hash"])
        except Exception as exc:
            logger.warning("Monitor anchor failed: %s", exc)
    report["blockchain_tx_hash"] = tx_hash

    set(key, report)
    return report


async def run_many(urls: list[str], concurrency: int | None = None) -> list[dict[str, Any]]:
    limit = concurrency or settings.MONITOR_CONCURRENCY_LIMIT
    semaphore = asyncio.Semaphore(limit)

    async def _bounded(url: str) -> dict[str, Any]:
        async with semaphore:
            return await run(url)

    tasks = [asyncio.create_task(_bounded(url)) for url in urls]
    return await asyncio.gather(*tasks)
