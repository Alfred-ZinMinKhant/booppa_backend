"""
Buyer-ladder scan quotas.

A "scan" = one unique vendor scanned this month at a given tier. Re-viewing
the same vendor within the same month is silently free (insert-or-noop via
the (buyer_id, vendor_id, month, scan_type) unique constraint on
VendorScanLedger).

Plan → tier-level limits live in app/billing/enforcement.py::BUYER_SCAN_LIMITS.

Helpers:
  consume_scan(db, buyer_id, plan, vendor_id, scan_type)
    Idempotent. Raises HTTPException(429) when the plan has a numeric limit
    and the buyer has already scanned `limit` distinct vendors this month
    at this tier and the new vendor isn't one of them. Returns a usage dict
    otherwise.

  scan_usage(db, buyer_id, plan)
    Returns a dict of {scan_type: {used, limit, remaining}} for the buyer's
    current calendar month. Used by /procurement/scan-quota for the dashboard
    widget.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing.enforcement import SCAN_TYPES, scan_limit_for
from app.core.models_v8 import VendorScanLedger

logger = logging.getLogger(__name__)


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def consume_scan(
    db: Session,
    buyer_id: Any,
    plan: str,
    vendor_id: Any,
    scan_type: str,
) -> dict[str, Any]:
    """
    Consume one scan credit for (buyer, vendor, month, scan_type).

    Idempotent: if a row already exists for this tuple, returns success
    without incrementing. If the plan has a finite limit and adding this
    new vendor would exceed it, raises HTTPException(429).

    Returns: {allowed, already_consumed, used, limit, remaining}
    """
    if scan_type not in SCAN_TYPES:
        raise HTTPException(400, f"Unknown scan_type: {scan_type}")

    limit = scan_limit_for(plan, scan_type)
    month = _current_month()

    # Was this vendor already consumed this month? If so, free reuse.
    existing = (
        db.query(VendorScanLedger.id)
        .filter(
            VendorScanLedger.buyer_id == buyer_id,
            VendorScanLedger.vendor_id == vendor_id,
            VendorScanLedger.month == month,
            VendorScanLedger.scan_type == scan_type,
        )
        .first()
    )

    used = (
        db.query(func.count(VendorScanLedger.id))
        .filter(
            VendorScanLedger.buyer_id == buyer_id,
            VendorScanLedger.month == month,
            VendorScanLedger.scan_type == scan_type,
        )
        .scalar()
        or 0
    )

    if existing:
        return {
            "allowed": True,
            "already_consumed": True,
            "used": used,
            "limit": limit,
            "remaining": (None if limit is None else max(0, limit - used)),
        }

    # New vendor — enforce quota before insert.
    if limit == 0:
        raise HTTPException(
            status_code=402,
            detail=(
                f"{scan_type.title()} Scans are not included in your plan. "
                f"Upgrade your Buyer tier to access this level of vendor scan."
            ),
        )
    if limit is not None and used >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly {scan_type.title()} Scan limit reached "
                f"({used}/{limit} unique vendors this month). "
                f"Resets on the 1st. Upgrade to increase your cap."
            ),
        )

    row = VendorScanLedger(
        id=uuid.uuid4(),
        buyer_id=buyer_id,
        vendor_id=vendor_id,
        month=month,
        scan_type=scan_type,
        plan_at_consumption=plan,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Race condition — another request consumed the same vendor between
        # the existence check and the insert. Treat as already-consumed.
        db.rollback()
        return {
            "allowed": True,
            "already_consumed": True,
            "used": used,
            "limit": limit,
            "remaining": (None if limit is None else max(0, limit - used)),
        }

    new_ledger_id = str(row.id)

    # On-chain per-scan verification log (Buyer Enterprise). Anchored async so it
    # never blocks the scan response. countdown lets the request's commit land
    # first; the task tolerates a not-yet-committed row by retrying.
    if (plan or "").lower().strip() in (
        "buyer_enterprise", "buyer_enterprise_monthly", "buyer_enterprise_annual",
    ):
        try:
            from app.workers.tasks import anchor_scan_ledger_task
            anchor_scan_ledger_task.apply_async(
                kwargs={"ledger_id": new_ledger_id}, countdown=5,
            )
        except Exception as e:  # never let anchoring break a paid scan
            logger.warning("[scan-anchor] could not enqueue anchor for %s: %s", new_ledger_id, e)

    return {
        "allowed": True,
        "already_consumed": False,
        "ledger_id": new_ledger_id,
        "used": used + 1,
        "limit": limit,
        "remaining": (None if limit is None else max(0, limit - used - 1)),
    }


def scan_usage(db: Session, buyer_id: Any, plan: str) -> dict[str, Any]:
    """Snapshot of the buyer's per-scan-type usage for the current month."""
    month = _current_month()
    rows = (
        db.query(VendorScanLedger.scan_type, func.count(VendorScanLedger.id))
        .filter(
            VendorScanLedger.buyer_id == buyer_id,
            VendorScanLedger.month == month,
        )
        .group_by(VendorScanLedger.scan_type)
        .all()
    )
    used_by_type = {st: int(c) for st, c in rows}

    summary = {}
    for st in SCAN_TYPES:
        limit = scan_limit_for(plan, st)
        used = used_by_type.get(st, 0)
        summary[st] = {
            "used": used,
            "limit": limit,
            "remaining": (None if limit is None else max(0, limit - used)),
        }
    return {"month": month, "plan": plan, "scans": summary}
