"""
acra_service.py — Offline ACRA register refresh into DiscoveredVendor
=====================================================================
Pulls the ACRA business-entities open dataset from data.gov.sg
(the same `settings.ACRA_DATASET_ID` the live lookup in
`evidence_enricher.fetch_acra_status` queries) and upserts LIVE entities
into the `discovered_vendors` table keyed on UEN.

This is the offline half of the ACRA integration: the live lookup handles
any single UEN/name on demand (cached 24h), while this monthly refresh
seeds `DiscoveredVendor` so the Vendor Proof registry-match path can hit a
local row without a network round-trip.

Consumers read `DiscoveredVendor.uen / entity_type / registration_date`
(see `app/services/fulfillment/single_products.py`), so we populate exactly
those fields. `MarketplaceVendor` is a separate, listing-oriented table and
is intentionally left alone here.

Reuses the production-grade paginated fetcher pattern from
`tasks.refresh_gebiz_base_rates._fetch_page` (retry on 429/5xx with
exponential backoff honouring Retry-After, polite inter-page delay).
"""

import asyncio
import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import get_async_client
from app.core.models import DiscoveredVendor

logger = logging.getLogger(__name__)

DATASTORE_URL = "https://data.gov.sg/api/action/datastore_search"

PAGE_SIZE = 1000
INTER_PAGE_DELAY = 0.6            # seconds between successful pages
MAX_PAGE_RETRIES = 4
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "BooppaBot/1.0 (+https://www.booppa.io)"

# Default safety cap. The full ACRA register is millions of rows; a monthly
# unbounded pull would balloon the table and the refresh runtime. The live
# lookup already covers arbitrary entities on demand, so the offline seed only
# needs a representative, bounded slice. Override via refresh_acra(max_records=).
DEFAULT_MAX_RECORDS = 50_000

# Entity types present in dataset d_3f960c10fed6145404ca7b821f263b87
# (entity_type_desc). All are legitimate business entities; we accept them all.
ACCEPTED_ENTITY_TYPES = {
    "LOCAL COMPANY",
    "SOLE PROPRIETORSHIP/ PARTNERSHIP",
    "SOLE PROPRIETORSHIP/PARTNERSHIP",
    "LIMITED LIABILITY PARTNERSHIP",
    "LIMITED PARTNERSHIP",
    "FOREIGN COMPANY BRANCH",
}

# uen_status_desc values that count as an active/live registration.
LIVE_STATUS_TOKENS = {"REGISTERED", "LIVE", "ACTIVE"}


def _field(rec: dict, *names: str) -> str:
    """First non-empty value among the given field-name variants."""
    for n in names:
        v = rec.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _infer_industry(text: str) -> Optional[str]:
    """Best-effort sector inference reusing the acra_import keyword map.

    This dataset carries no SSIC/activity field, so inference runs on the
    entity name alone and will often land on "Other" — acceptable for a seed.
    Falls back to None if the import script isn't importable in this context.
    """
    try:
        from scripts.acra_import import infer_sector
    except Exception:
        return None
    try:
        sector = infer_sector(text)
        return sector if sector and sector != "Other" else None
    except Exception:
        return None


def _normalize(rec: dict) -> Optional[dict]:
    """Map a raw datastore record to DiscoveredVendor columns, or None to skip.

    Skips non-accepted entity types and non-live registrations so the offline
    table mirrors what the live lookup would report as `live=True`.
    """
    uen = _field(rec, "uen")
    name = _field(rec, "entity_name", "company_name")
    if not uen or not name:
        return None

    entity_type = _field(rec, "entity_type_desc", "entity_type")
    if entity_type and entity_type.upper() not in ACCEPTED_ENTITY_TYPES:
        return None

    status = _field(rec, "uen_status_desc", "entity_status", "status")
    if status and status.upper() not in LIVE_STATUS_TOKENS:
        return None

    return {
        "uen": uen,
        "company_name": name,
        "entity_type": entity_type or None,
        "registration_date": _field(rec, "uen_issue_date", "incorporation_date") or None,
        "industry": _infer_industry(name),
        "country": "Singapore",
        "source": "acra",
    }


async def _fetch_page(client, dataset_id: str, offset: int) -> Optional[dict]:
    """GET one datastore page, retrying on 429/5xx/network errors with
    exponential backoff (honouring Retry-After). Returns parsed JSON, or None
    when the page can't be fetched after all retries."""
    backoff = 2.0
    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        try:
            resp = await client.get(
                DATASTORE_URL,
                params={"resource_id": dataset_id, "limit": PAGE_SIZE, "offset": offset},
                headers={"User-Agent": USER_AGENT},
            )
        except Exception as e:
            if attempt == MAX_PAGE_RETRIES:
                logger.warning(
                    "[ACRA] network error dataset=%s offset=%s after %s tries: %s",
                    dataset_id, offset, attempt, e,
                )
                return None
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_PAGE_RETRIES:
            ra = resp.headers.get("Retry-After")
            try:
                wait_s = float(ra) if ra else backoff
            except (TypeError, ValueError):
                wait_s = backoff
            wait_s = min(wait_s, 30.0)
            logger.info(
                "[ACRA] HTTP %s dataset=%s offset=%s — retry %s/%s in %.0fs",
                resp.status_code, dataset_id, offset, attempt, MAX_PAGE_RETRIES, wait_s,
            )
            await asyncio.sleep(wait_s)
            backoff *= 2
            continue

        logger.warning(
            "[ACRA] HTTP %s dataset=%s offset=%s — giving up",
            resp.status_code, dataset_id, offset,
        )
        return None
    return None


async def _refresh_async(db, dataset_id: str, max_records: Optional[int]) -> int:
    """Paginate the dataset and upsert live entities into DiscoveredVendor.

    Returns the number of rows inserted or updated. Upserts in batches via a
    Postgres ON CONFLICT(uen) so a re-run is idempotent and cheap.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    upserted = 0
    scanned = 0
    offset = 0
    batch: list[dict] = []
    seen_uens: set[str] = set()

    def _flush() -> int:
        nonlocal batch
        if not batch:
            return 0
        stmt = pg_insert(DiscoveredVendor.__table__).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["uen"],
            set_={
                "company_name": stmt.excluded.company_name,
                "entity_type": stmt.excluded.entity_type,
                "registration_date": stmt.excluded.registration_date,
                "industry": stmt.excluded.industry,
                "source": stmt.excluded.source,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        db.execute(stmt)
        db.commit()
        n = len(batch)
        batch = []
        return n

    from datetime import datetime as _dt

    async with get_async_client(timeout=30.0) as client:
        while True:
            data = await _fetch_page(client, dataset_id, offset)
            if data is None:
                break
            records = (data.get("result") or {}).get("records") or []
            if not records:
                break

            for rec in records:
                scanned += 1
                row = _normalize(rec)
                if row is None:
                    continue
                if row["uen"] in seen_uens:
                    continue
                seen_uens.add(row["uen"])
                row["updated_at"] = _dt.utcnow()
                batch.append(row)
                if len(batch) >= 500:
                    upserted += _flush()

            if max_records and upserted + len(batch) >= max_records:
                break

            offset += PAGE_SIZE
            await asyncio.sleep(INTER_PAGE_DELAY)

    upserted += _flush()
    logger.info(
        "[ACRA] refresh complete: dataset=%s scanned=%s upserted=%s",
        dataset_id, scanned, upserted,
    )
    return upserted


def refresh_acra(db, dataset_id: Optional[str] = None,
                 max_records: Optional[int] = DEFAULT_MAX_RECORDS) -> int:
    """Refresh the offline ACRA seed in `discovered_vendors`.

    Synchronous entry point (drives the async paginated fetch internally) so it
    can be called from a Celery task or a one-off script with a plain DB
    session. Returns the number of DiscoveredVendor rows inserted/updated.
    """
    dataset_id = dataset_id or settings.ACRA_DATASET_ID
    return asyncio.run(_refresh_async(db, dataset_id, max_records))
