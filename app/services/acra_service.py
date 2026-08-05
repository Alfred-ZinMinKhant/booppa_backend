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
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from sqlalchemy import func

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


def registry_live_clause():
    """SQL clause: `DiscoveredVendor` rows safe to show in a vendor listing.

    The targeted refresh pass deliberately keeps ceased/struck-off entities (for a
    UEN a customer actually looked up, "struck off" is the fact worth surfacing),
    which means `discovered_vendors` is no longer a live-only table. Any endpoint
    that BROWSES the table as a vendor directory must apply this, or a struck-off
    company shows up as a discoverable vendor.

    NULL passes: it means "never targeted-checked", which covers every row written
    by the bulk sweep — and the sweep already dropped non-live records upstream in
    `_normalize`. Token matching mirrors that same exact-match `LIVE_STATUS_TOKENS`
    test so the two passes cannot disagree about what "live" means.

    Lookups BY a specific UEN are not listings and should not use this — there the
    ceased status is the answer, not a reason to hide the row.
    """
    from sqlalchemy import func, or_

    return or_(
        DiscoveredVendor.registry_status.is_(None),
        func.upper(DiscoveredVendor.registry_status).in_(LIVE_STATUS_TOKENS),
    )


def collect_known_uens(db) -> list[str]:
    """Every UEN that appears anywhere in Booppa's own data, uppercased + deduped.

    This is the selection criterion for the targeted ACRA pass. The full register is
    1.5M+ entities and the unfiltered sweep's `DEFAULT_MAX_RECORDS` slice is an
    arbitrary offset-ordered 50k of them; neither is "the companies our customers
    actually look at". This is.

    Every source is queried independently and unioned in Python rather than as one
    SQL UNION: the tables live in unrelated feature areas, several are large, and a
    single failing source should not take down the whole collection. A source that
    raises is logged and skipped — a short list is a smaller backfill, not a broken
    one.
    """
    from app.core.models import (
        CspProfile,
        LeadCapture,
        ManagedEntity,
        ManagedVendor,
        MarketplaceVendor,
        PendingRfpIntake,
        Report,
        Subsidiary,
        User,
    )

    uens: set[str] = set()

    def _add(label: str, query_fn) -> None:
        try:
            rows = query_fn()
        except Exception as e:  # noqa: BLE001 - one bad source must not sink the rest
            logger.warning("[ACRA] collect_known_uens: source %s failed: %s", label, e)
            return
        before = len(uens)
        for (value,) in rows:
            if value and str(value).strip():
                uens.add(str(value).strip().upper())
        logger.debug("[ACRA] collect_known_uens: %s contributed %s new", label, len(uens) - before)

    for label, model in (
        ("User", User),
        ("ManagedEntity", ManagedEntity),
        ("MarketplaceVendor", MarketplaceVendor),
        ("DiscoveredVendor", DiscoveredVendor),
        ("CspProfile", CspProfile),
        ("Subsidiary", Subsidiary),
        ("PendingRfpIntake", PendingRfpIntake),
        ("LeadCapture", LeadCapture),
    ):
        _add(label, lambda m=model: db.query(m.uen).filter(m.uen.isnot(None)).distinct().all())

    # `ManagedVendor` carries no `uen` column of its own — the vendor's UEN lives on
    # the linked user account, and `vendor_user_id` is NULL for not-yet-registered
    # invitees, so this is an inner join by design (nothing to look up otherwise).
    _add(
        "ManagedVendor",
        lambda: db.query(User.uen)
        .join(ManagedVendor, ManagedVendor.vendor_user_id == User.id)
        .filter(User.uen.isnot(None))
        .distinct()
        .all(),
    )

    # `assessment_data` is a `json` column, so `cast(col['uen'], String)` would
    # silently never match — `.as_string()` is the only form that compares. Rows
    # without a "uen" key yield SQL NULL and are dropped here rather than in Python.
    _add(
        "Report.assessment_data",
        lambda: db.query(Report.assessment_data["uen"].as_string())
        .filter(Report.assessment_data["uen"].as_string().isnot(None))
        .distinct()
        .all(),
    )

    result = sorted(uens)
    logger.info("[ACRA] collect_known_uens: %s distinct UENs across all sources", len(result))
    return result


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


def _normalize(rec: dict, *, require_live: bool = True) -> Optional[dict]:
    """Map a raw datastore record to DiscoveredVendor columns, or None to skip.

    `require_live=True` (the bulk sweep) skips non-accepted entity types and
    non-live registrations so the offline table mirrors what the live lookup would
    report as `live=True`.

    `require_live=False` (the targeted pass) keeps every record that has a UEN and
    a name, and reports the raw registry status in `registry_status`. For a UEN a
    customer actually looked up, "struck off" is the single most valuable fact in
    the register — dropping it leaves the caller unable to distinguish a ceased
    company from one we simply never fetched.

    Both modes share this one field-mapping body on purpose: the dataset's column
    names are the fragile part, and two copies would drift.
    """
    uen = _field(rec, "uen")
    name = _field(rec, "entity_name", "company_name")
    if not uen or not name:
        return None

    entity_type = _field(rec, "entity_type_desc", "entity_type")
    status = _field(rec, "uen_status_desc", "entity_status", "status")

    if require_live:
        if entity_type and entity_type.upper() not in ACCEPTED_ENTITY_TYPES:
            return None
        if status and status.upper() not in LIVE_STATUS_TOKENS:
            return None

    row = {
        "uen": uen,
        "company_name": name,
        "entity_type": entity_type or None,
        "registration_date": _field(rec, "uen_issue_date", "incorporation_date") or None,
        "industry": _infer_industry(name),
        "country": "Singapore",
        "source": "acra",
    }

    if not require_live:
        # Only the targeted pass may write these. The sweep sees live records
        # exclusively, so anything it wrote would be an unearned positive claim.
        row["registry_status"] = status or None
        row["registry_checked_at"] = _dt_now()

    return row


def _dt_now():
    from datetime import datetime

    return datetime.utcnow()


async def _get_with_retry(client, params: dict, label: str) -> Optional[dict]:
    """GET one datastore request, retrying on 429/5xx/network errors with
    exponential backoff (honouring Retry-After). Returns parsed JSON, or None
    when the request can't be satisfied after all retries.

    `label` only identifies the request in logs. Both the paginated sweep and the
    targeted UEN fetch go through here so there is exactly one place defining how
    politely we treat data.gov.sg.
    """
    backoff = 2.0
    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        try:
            resp = await client.get(
                DATASTORE_URL, params=params, headers={"User-Agent": USER_AGENT}
            )
        except Exception as e:
            if attempt == MAX_PAGE_RETRIES:
                logger.warning(
                    "[ACRA] network error %s after %s tries: %s", label, attempt, e
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
                "[ACRA] HTTP %s %s — retry %s/%s in %.0fs",
                resp.status_code, label, attempt, MAX_PAGE_RETRIES, wait_s,
            )
            await asyncio.sleep(wait_s)
            backoff *= 2
            continue

        logger.warning("[ACRA] HTTP %s %s — giving up", resp.status_code, label)
        return None
    return None


async def _fetch_page(client, dataset_id: str, offset: int) -> Optional[dict]:
    """GET one page of the full dataset sweep."""
    return await _get_with_retry(
        client,
        {"resource_id": dataset_id, "limit": PAGE_SIZE, "offset": offset},
        f"dataset={dataset_id} offset={offset}",
    )


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
        # `set_` is a deliberate allowlist, not "every column". `verified_hint` /
        # `verified_at` are written by fulfillment when a scan verifies a subject
        # (evidence_enricher.VENDOR_CARRIER); listing them here would wipe that
        # provenance on every registry refresh and make every vendor row look
        # unprovenanced — i.e. permanently untrusted.
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


TARGETED_BATCH_SIZE = 50
TARGETED_PROBE_SIZE = 5

# Per-run wall-clock budget. Sized against the GLOBAL Celery limits in
# `celery_app.py` (`task_soft_time_limit=250`), leaving room for
# `collect_known_uens` and the final flush. It is deliberately not a bigger
# timeout: `booppa-worker` runs concurrency 2 across BOTH queues, so a
# multi-hour task would hold half the fulfillment capacity for hours. Short
# resumable passes give the slot back between links instead.
TARGETED_RUN_BUDGET_SECONDS = 180


async def _probe_batching(client, dataset_id: str, uens: list[str]) -> bool:
    """Does this datastore honour a LIST-valued `filters` (IN semantics)?

    CKAN documents it, but the only usage proven in this repo sends a single UEN
    (`evidence_enricher.py:201`), so we do not assume. Send a small list and check
    the response actually spans more than one distinct UEN. A server that ignores
    the list form typically matches nothing or echoes one row — either way we must
    fall back, because silently accepting a 1-row answer for a 50-UEN request would
    look like "49 companies not in ACRA" rather than like a broken query.
    """
    probe = uens[:TARGETED_PROBE_SIZE]
    if len(probe) < 2:
        return False
    data = await _get_with_retry(
        client,
        {
            "resource_id": dataset_id,
            "limit": 100,
            "filters": json.dumps({"uen": probe}),
        },
        f"batch-probe n={len(probe)}",
    )
    if not data:
        return False
    records = (data.get("result") or {}).get("records") or []
    distinct = {_field(r, "uen").upper() for r in records if _field(r, "uen")}
    return len(distinct) > 1


async def _iter_fetch(client, dataset_id: str, uens: list[str], batched: bool):
    """Yield `(chunk, records)` one request at a time.

    A generator rather than a "fetch everything, then write" function so the
    caller can persist each chunk as it lands and stop on a deadline. The first
    production run did it the other way round, spent its whole time budget
    fetching, and was cut off before a single row reached the database — 250
    seconds of load on data.gov.sg for nothing.
    """
    step = TARGETED_BATCH_SIZE if batched else 1
    for i in range(0, len(uens), step):
        chunk = uens[i : i + step]
        params = (
            {"resource_id": dataset_id, "limit": len(chunk) * 2,
             "filters": json.dumps({"uen": chunk})}
            if batched else
            {"resource_id": dataset_id, "limit": 1,
             "filters": json.dumps({"uen": chunk[0]})}
        )
        label = f"targeted chunk n={len(chunk)}" if batched else f"targeted uen={chunk[0]}"
        data = await _get_with_retry(client, params, label)
        records = (data.get("result") or {}).get("records") or [] if data else []
        yield chunk, records
        await asyncio.sleep(INTER_PAGE_DELAY)


async def _fetch_by_uens(client, dataset_id: str, uens: list[str]) -> tuple[list[dict], bool]:
    """Fetch raw ACRA records for a specific list of UENs.

    Returns `(records, batched)`. Uses the batched `filters` list form when the
    probe shows the datastore honours it, otherwise one request per UEN. This
    collects the whole result in memory and so has no deadline — it is the
    convenience form for tests and small explicit UEN lists, not the path the
    scheduled backfill takes.
    """
    batched = await _probe_batching(client, dataset_id, uens)
    logger.info(
        "[ACRA] targeted fetch: %s UENs, mode=%s",
        len(uens), "batched" if batched else "per-UEN",
    )
    records: list[dict] = []
    async for _chunk, recs in _iter_fetch(client, dataset_id, uens, batched):
        records.extend(recs)
    return records, batched


def _order_for_resume(db, uens: list[str]) -> list[str]:
    """Order UENs so a resumed run continues rather than repeats.

    Never-checked UENs first (`registry_checked_at IS NULL`, or no
    `discovered_vendors` row at all), then the stalest. Because each run is
    deadline-bounded, this ordering is what makes a sequence of short runs
    converge on full coverage instead of re-fetching the same head of the list
    every time.

    Falls back to the natural order if the lookup fails — a worse order still
    makes progress, whereas raising here would block the backfill entirely.
    """
    from datetime import datetime

    if not uens:
        return []
    try:
        rows = (
            db.query(DiscoveredVendor.uen, DiscoveredVendor.registry_checked_at)
            .filter(func.upper(DiscoveredVendor.uen).in_(uens))
            .all()
        )
    except Exception as e:
        logger.warning("[ACRA] resume ordering unavailable, using natural order: %s", e)
        return list(uens)
    checked = {
        (u or "").strip().upper(): ts for u, ts in rows if ts is not None
    }
    return sorted(
        uens,
        key=lambda u: (u in checked, checked.get(u) or datetime.min, u),
    )


def _upsert_targeted(db, rows: list[dict]) -> int:
    """Upsert one batch of targeted rows. Returns the number written."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i : i + 500]
        stmt = pg_insert(DiscoveredVendor.__table__).values(chunk)
        # A SEPARATE allowlist from the sweep's, deliberately. This one writes
        # `registry_status` / `registry_checked_at`; the sweep's must never list
        # them, or the next monthly run would overwrite a targeted "Struck Off"
        # with a stale value. `verified_hint` / `verified_at` stay out of both —
        # they are fulfillment's provenance writes (see `_flush`).
        stmt = stmt.on_conflict_do_update(
            index_elements=["uen"],
            set_={
                "company_name": stmt.excluded.company_name,
                "entity_type": stmt.excluded.entity_type,
                "registration_date": stmt.excluded.registration_date,
                "industry": stmt.excluded.industry,
                "source": stmt.excluded.source,
                "registry_status": stmt.excluded.registry_status,
                "registry_checked_at": stmt.excluded.registry_checked_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        db.execute(stmt)
        db.commit()
        written += len(chunk)
    return written


@asynccontextmanager
async def _targeted_client(client=None):
    """Yield `client` if one was supplied, else open (and close) our own.

    Injection point for tests: without it a test that passes a fake client is
    silently ignored and the "unit" test hits data.gov.sg for real.
    """
    if client is not None:
        yield client
        return
    async with get_async_client(timeout=30.0) as owned:
        yield owned


async def _refresh_targeted_async(db, dataset_id: str, uens: list[str],
                                  budget_seconds: float, client=None) -> dict:
    """Fetch and upsert ACRA records for `uens`, keeping ceased entities.

    Stops cleanly once `budget_seconds` is spent and reports `complete: False`
    with the UENs it never reached. It writes each chunk as it arrives, so a run
    that ends early — on its own deadline or on a worker restart — leaves real
    coverage behind rather than nothing.
    """
    empty = {"requested": 0, "matched": 0, "upserted": 0, "unmatched": 0,
             "batched": False, "processed": 0, "remaining": 0, "complete": True}
    if not uens:
        return empty

    started = time.monotonic()
    matched: set[str] = set()
    processed: list[str] = []
    upserted = 0
    requested = set(uens)

    async with _targeted_client(client) as client:
        batched = await _probe_batching(client, dataset_id, uens)
        logger.info(
            "[ACRA] targeted fetch: %s UENs, mode=%s, budget=%ss",
            len(uens), "batched" if batched else "per-UEN", budget_seconds,
        )
        async for chunk, records in _iter_fetch(client, dataset_id, uens, batched):
            rows: dict[str, dict] = {}
            for rec in records:
                row = _normalize(rec, require_live=False)
                if row is None:
                    continue
                key = row["uen"].upper()
                # Only keep records we actually asked for. A datastore that
                # ignores the filter would otherwise let arbitrary rows in.
                if key not in requested:
                    continue
                row["updated_at"] = _dt_now()
                rows[key] = row

            upserted += _upsert_targeted(db, list(rows.values()))
            matched.update(rows)
            processed.extend(chunk)

            if time.monotonic() - started >= budget_seconds:
                break

    remaining = len(uens) - len(processed)
    result = {
        "requested": len(processed),
        "matched": len(matched),
        "upserted": upserted,
        # Scoped to what this run actually processed: a UEN we never reached is
        # not evidence of absence from the register, and counting it as
        # "unmatched" would overstate how many of our UENs ACRA cannot find.
        "unmatched": len(processed) - len(matched),
        "batched": batched,
        "processed": len(processed),
        "remaining": remaining,
        "complete": remaining == 0,
    }
    logger.info("[ACRA] targeted refresh pass: %s", result)
    return result


def refresh_acra_targeted(db, uens: Optional[list[str]] = None,
                          dataset_id: Optional[str] = None,
                          budget_seconds: float = TARGETED_RUN_BUDGET_SECONDS) -> dict:
    """Refresh ACRA facts for UENs Booppa has actually touched.

    Runs ALONGSIDE `refresh_acra`, never replacing it: the unfiltered sweep still
    seeds a local fallback for an arbitrary new lookup, while this pass gives the
    companies our customers really look at a real registration date and a current
    registry status.

    One call is one bounded pass, not the whole backfill. `budget_seconds` keeps
    it well inside the global Celery soft limit, and `_order_for_resume` means
    consecutive passes advance through the list instead of restarting it — so
    ~45k UENs complete over a sequence of short runs that each release the worker
    slot back to paid fulfillment work.

    `uens` defaults to `collect_known_uens(db)`. Returns coverage counts —
    `{"requested", "matched", "upserted", "unmatched", "batched", "processed",
    "remaining", "complete"}` — because "matched" is the evidence for whether a
    "registered since" claim is worth surfacing at all, and "remaining" is what
    tells the caller whether to run again.
    """
    dataset_id = dataset_id or settings.ACRA_DATASET_ID
    if uens is None:
        uens = collect_known_uens(db)
    uens = sorted({u.strip().upper() for u in uens if u and str(u).strip()})
    uens = _order_for_resume(db, uens)
    return asyncio.run(_refresh_targeted_async(db, dataset_id, uens, budget_seconds))


EMPTY_REGISTRY_FACTS = {"registered_since": None, "registry_status": None}


def registered_since_year(registration_date) -> Optional[int]:
    """The 4-digit year from a raw ACRA registration date, or None.

    `registration_date` is stored exactly as ACRA wrote it (`acra_service` never
    parses it), and the observed forms vary. A year is the narrowest claim the
    raw string can actually support, so that is all we ever return — a full date
    would be asserting a precision we have not verified.
    """
    if not registration_date:
        return None
    match = re.search(r"(1[89]\d{2}|20\d{2})", str(registration_date))
    if not match:
        return None
    return int(match.group(1))


def resolve_registry_facts_bulk(db, uens) -> dict:
    """Map UEN -> `{"registered_since", "registry_status"}` for the given UENs.

    Every requested UEN gets an entry, including ones with no `DiscoveredVendor`
    row at all, so callers never have to distinguish "absent from the map" from
    "absent from the register".

    `registry_status` is None whenever the targeted pass has not checked this UEN
    (`registry_status IS NULL`). None means UNKNOWN, not live — renderers must
    omit the field entirely rather than defaulting it to a positive status. The
    bulk form exists because the portfolio serializers run over whole lists; a
    per-row call there is an N+1.
    """
    wanted = sorted({u.strip().upper() for u in uens if u and str(u).strip()})
    facts = {u: dict(EMPTY_REGISTRY_FACTS) for u in wanted}
    if not wanted:
        return facts
    try:
        rows = (
            db.query(
                DiscoveredVendor.uen,
                DiscoveredVendor.registration_date,
                DiscoveredVendor.registry_status,
            )
            .filter(func.upper(DiscoveredVendor.uen).in_(wanted))
            .all()
        )
    except Exception as e:
        # A profile must still render when the registry lookup fails. Absent
        # facts are the honest fallback; raising would take down the whole page
        # over a supplementary field.
        logger.warning(f"resolve_registry_facts_bulk failed: {e}")
        return facts
    for uen, reg_date, status in rows:
        facts[uen.strip().upper()] = {
            "registered_since": registered_since_year(reg_date),
            "registry_status": status or None,
        }
    return facts


def resolve_registry_facts(db, uen: Optional[str]) -> dict:
    """Single-UEN form of `resolve_registry_facts_bulk`. Never raises."""
    if not uen:
        return dict(EMPTY_REGISTRY_FACTS)
    return resolve_registry_facts_bulk(db, [uen])[uen.strip().upper()]


def refresh_acra(db, dataset_id: Optional[str] = None,
                 max_records: Optional[int] = DEFAULT_MAX_RECORDS) -> int:
    """Refresh the offline ACRA seed in `discovered_vendors`.

    Synchronous entry point (drives the async paginated fetch internally) so it
    can be called from a Celery task or a one-off script with a plain DB
    session. Returns the number of DiscoveredVendor rows inserted/updated.
    """
    dataset_id = dataset_id or settings.ACRA_DATASET_ID
    return asyncio.run(_refresh_async(db, dataset_id, max_records))
