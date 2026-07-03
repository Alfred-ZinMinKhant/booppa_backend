"""Buyer (procurement) insight builders — the substance behind the buyer digest.

The buyer analog of `vendor_active_insights`. Read-only, best-effort helpers that
turn the one recurring buyer asset — the org watchlist (`VendorWatchlistItem`) —
into monitored intelligence: each watched supplier's current verification status,
Trust/Compliance score, month-over-month drift, and risk signal.

Resolution chain for a watchlist row:
    VendorWatchlistItem.vendor_ref  (marketplace slug or free-form id)
        → MarketplaceVendor.slug → claimed_by_user_id (the vendor's User)
            → VendorScore / VendorStatusSnapshot / ScoreSnapshot history

Unclaimed / unresolvable suppliers degrade to an "unrated" row rather than being
dropped, so the buyer still sees the full watchlist. Every function swallows its
own exceptions and returns [] / None so a single data gap can never block an email
or a PDF. Nothing here writes to the DB.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Risk signals we surface as "needs attention" in the digest headline.
_ALERT_RISK_SIGNALS = {"FLAGGED", "CRITICAL"}


def get_buyer_org_ids(db, user_id: str) -> list[str]:
    """Every organisation the buyer belongs to (member or owner). [] when none."""
    try:
        from app.core.models_enterprise import Organisation, OrganisationMember

        ids: set[str] = set()
        for m in (
            db.query(OrganisationMember)
            .filter(OrganisationMember.user_id == user_id)
            .all()
        ):
            ids.add(str(m.organisation_id))
        # Owner may predate an explicit membership row.
        for o in (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user_id)
            .all()
        ):
            ids.add(str(o.id))
        return list(ids)
    except Exception as e:  # pragma: no cover
        logger.warning("[BuyerInsights] get_buyer_org_ids failed for %s: %s", user_id, e)
        return []


def _resolve_watchlist_vendor_user(db, vendor_ref: str) -> str | None:
    """Resolve a watchlist `vendor_ref` to a registered vendor User id, or None.

    A ref is a MarketplaceVendor slug when the supplier is in the directory; the
    score/status only exist once that marketplace profile is claimed by a User.
    """
    try:
        from app.core.models_v10 import MarketplaceVendor

        mv = (
            db.query(MarketplaceVendor)
            .filter(MarketplaceVendor.slug == vendor_ref)
            .first()
        )
        if mv and mv.claimed_by_user_id:
            return str(mv.claimed_by_user_id)
    except Exception as e:  # pragma: no cover
        logger.warning("[BuyerInsights] resolve vendor_ref=%s failed: %s", vendor_ref, e)
    return None


def _supplier_status(db, vendor_user_id: str) -> dict:
    """Current status/score snapshot for a resolved vendor User. Best-effort."""
    from app.core.models_v6 import VendorScore
    from app.core.models_v8 import VendorStatusSnapshot
    from app.services.vendor_active_insights import get_score_trend

    out: dict = {
        "trust_score": None,
        "compliance_score": None,
        "trust_delta": None,
        "compliance_delta": None,
        "risk_signal": None,
        "procurement_readiness": None,
    }
    try:
        sc = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_user_id).first()
        if sc:
            out["trust_score"] = sc.total_score
            out["compliance_score"] = sc.compliance_score
    except Exception:
        pass
    try:
        snap = (
            db.query(VendorStatusSnapshot)
            .filter(VendorStatusSnapshot.vendor_id == vendor_user_id)
            .first()
        )
        if snap:
            out["risk_signal"] = snap.risk_signal
            out["procurement_readiness"] = snap.procurement_readiness
    except Exception:
        pass
    try:
        trend = get_score_trend(db, vendor_user_id)
        if trend:
            out["trust_delta"] = trend.get("total_delta")
            out["compliance_delta"] = trend.get("compliance_delta")
    except Exception:
        pass
    return out


def get_watched_suppliers_with_status(db, user_id: str, limit: int = 50) -> list[dict]:
    """Every supplier on the buyer's org watchlist(s), annotated with live status.

    Returns [] when the buyer has no org / empty watchlist (Starter single-seat is
    the common case) — the caller falls back to the tender section. Rows carry
    `resolved=False` when the supplier isn't a claimed marketplace profile yet.
    Ordered so alerting suppliers (FLAGGED/CRITICAL) surface first.
    """
    try:
        from app.core.models_enterprise import VendorWatchlistItem

        org_ids = get_buyer_org_ids(db, user_id)
        if not org_ids:
            return []

        items = (
            db.query(VendorWatchlistItem)
            .filter(VendorWatchlistItem.organisation_id.in_(org_ids))
            .order_by(VendorWatchlistItem.created_at.desc())
            .limit(limit)
            .all()
        )
        rows: list[dict] = []
        seen: set[str] = set()
        for it in items:
            if it.vendor_ref in seen:
                continue
            seen.add(it.vendor_ref)
            row = {
                "vendor_ref": it.vendor_ref,
                "vendor_name": it.vendor_name or it.vendor_ref,
                "notes": it.notes,
                "resolved": False,
            }
            vuid = _resolve_watchlist_vendor_user(db, it.vendor_ref)
            if vuid:
                row["resolved"] = True
                row.update(_supplier_status(db, vuid))
            rows.append(row)

        def _alerting(r) -> int:
            return 0 if (r.get("risk_signal") in _ALERT_RISK_SIGNALS) else 1

        rows.sort(key=_alerting)
        return rows
    except Exception as e:  # pragma: no cover
        logger.warning("[BuyerInsights] get_watched_suppliers_with_status failed for %s: %s", user_id, e)
        return []


def summarise_watchlist(rows: list[dict]) -> dict:
    """Headline counts for the digest subject line / email intro."""
    total = len(rows)
    alerting = [r for r in rows if r.get("risk_signal") in _ALERT_RISK_SIGNALS]
    slipped = [r for r in rows if isinstance(r.get("trust_delta"), int) and r["trust_delta"] < 0]
    return {
        "total": total,
        "alerting": len(alerting),
        "slipped": len(slipped),
        "alerting_names": [r.get("vendor_name") for r in alerting][:5],
    }
