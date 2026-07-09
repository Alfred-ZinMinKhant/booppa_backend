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

# Event-triggered drift-alert thresholds (#1).
DRIFT_SCORE_DROP = 5          # trust-score points dropped since last alert → alert
CERT_EXPIRY_WARN_DAYS = 30    # warn when a supplier's cert lapses within this window


def get_buyer_org_ids(db, user_id: str) -> list[str]:
    """Every organisation the buyer belongs to (member or owner). [] when none."""
    try:
        from app.core.models import Organisation, OrganisationMember

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
        from app.core.models import MarketplaceVendor

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
    from app.core.models import VendorScore
    from app.core.models import VendorStatusSnapshot
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


def get_supplier_cert_expiry(db, vendor_user_id: str):
    """Nearest active certificate expiry for a resolved vendor, or None.

    Sourced from VerifyRecord.expires_at — the Vendor Proof / verification cert the
    buyer is relying on. Best-effort; returns None on any gap.
    """
    try:
        from app.core.models import VerifyRecord

        rec = (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.vendor_id == vendor_user_id,
                VerifyRecord.expires_at.isnot(None),
            )
            .order_by(VerifyRecord.expires_at.asc())
            .first()
        )
        return rec.expires_at if rec else None
    except Exception as e:  # pragma: no cover
        logger.warning("[BuyerInsights] cert expiry lookup failed for %s: %s", vendor_user_id, e)
        return None


def evaluate_supplier_drift(current: dict, cert_expiry, ledger) -> dict | None:
    """Decide whether a watched supplier's change warrants an *immediate* alert.

    Pure comparison of the live status (`current` from `_supplier_status`, plus the
    nearest `cert_expiry` datetime) against the last state we alerted on (`ledger`,
    a BuyerSupplierAlert row or None). Returns a dict describing the alert, or None
    when nothing material crossed a threshold.

    Reasons, in priority order:
      * ``risk_flip``   — flipped *into* FLAGGED/CRITICAL (and wasn't already there).
      * ``score_drop``  — trust score fell ≥ DRIFT_SCORE_DROP since the last alert.
      * ``cert_expiry`` — an active certificate lapses within CERT_EXPIRY_WARN_DAYS
                          and we haven't already warned for that same expiry date.
    """
    from datetime import datetime, timedelta, timezone

    last_signal = getattr(ledger, "last_risk_signal", None) if ledger else None
    last_score = getattr(ledger, "last_trust_score", None) if ledger else None
    last_expiry_warned = getattr(ledger, "last_expiry_warned_for", None) if ledger else None

    cur_signal = current.get("risk_signal")
    cur_score = current.get("trust_score")

    # 1) Risk flip into an alerting signal.
    if cur_signal in _ALERT_RISK_SIGNALS and last_signal not in _ALERT_RISK_SIGNALS:
        return {
            "reason": "risk_flip",
            "headline": f"Risk signal changed to {cur_signal}",
            "detail": (
                f"This supplier's risk signal is now <strong>{cur_signal}</strong>. "
                "We flagged it the moment it changed so you can reassess before your "
                "next procurement decision."
            ),
            "risk_signal": cur_signal,
            "trust_score": cur_score,
        }

    # 2) Material trust-score drop since we last alerted (needs both scores).
    if (
        isinstance(cur_score, int)
        and isinstance(last_score, int)
        and (last_score - cur_score) >= DRIFT_SCORE_DROP
    ):
        drop = last_score - cur_score
        return {
            "reason": "score_drop",
            "headline": f"Trust score dropped {drop} points",
            "detail": (
                f"This supplier's Trust score fell from <strong>{last_score}</strong> to "
                f"<strong>{cur_score}</strong> ({drop} points). A sustained drop can signal "
                "lapsing verification or a new risk finding."
            ),
            "risk_signal": cur_signal,
            "trust_score": cur_score,
        }

    # 3) Approaching certificate expiry (warn once per distinct expiry date).
    if cert_expiry is not None:
        now = datetime.now(timezone.utc)
        exp = cert_expiry
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        within = exp <= (now + timedelta(days=CERT_EXPIRY_WARN_DAYS))
        already = (
            last_expiry_warned is not None
            and abs((last_expiry_warned - cert_expiry).total_seconds()) < 86400
        )
        if within and exp > now and not already:
            days = max(0, (exp - now).days)
            return {
                "reason": "cert_expiry",
                "headline": f"Certificate expires in {days} day{'s' if days != 1 else ''}",
                "detail": (
                    f"This supplier's verification certificate lapses on "
                    f"<strong>{exp.strftime('%d %b %Y')}</strong> ({days} days). "
                    "Ask them to renew before it expires to keep your audit trail current."
                ),
                "risk_signal": cur_signal,
                "trust_score": cur_score,
                "expiry": cert_expiry,
            }

    return None


def get_watchlist_sectors(db, user_id: str) -> dict[str, list[str]]:
    """Map each sector the buyer's watched suppliers operate in → the supplier names.

    The buyer-fit signal for a tender: buyers watch suppliers, and those suppliers
    carry `VendorSector` tags. A tender in a sector the buyer already sources from
    is a strong fit. Resolves each watchlist row to its vendor User, then to every
    `VendorSector` for that vendor. Best-effort — returns {} on any gap or when the
    buyer watches nothing resolvable.
    """
    out: dict[str, list[str]] = {}
    try:
        from app.core.models import VendorWatchlistItem
        from app.core.models import VendorSector

        org_ids = get_buyer_org_ids(db, user_id)
        if not org_ids:
            return {}

        items = (
            db.query(VendorWatchlistItem)
            .filter(VendorWatchlistItem.organisation_id.in_(org_ids))
            .all()
        )
        seen_refs: set[str] = set()
        for it in items:
            if it.vendor_ref in seen_refs:
                continue
            seen_refs.add(it.vendor_ref)
            vuid = _resolve_watchlist_vendor_user(db, it.vendor_ref)
            if not vuid:
                continue
            name = (it.vendor_name or it.vendor_ref or "").strip()
            for sv in db.query(VendorSector).filter(VendorSector.vendor_id == vuid).all():
                sector = (sv.sector or "").strip()
                if not sector:
                    continue
                names = out.setdefault(sector, [])
                if name and name not in names:
                    names.append(name)
    except Exception as e:  # pragma: no cover
        logger.warning("[BuyerInsights] get_watchlist_sectors failed for %s: %s", user_id, e)
        return {}
    return out


def get_watched_suppliers_with_status(db, user_id: str, limit: int = 50) -> list[dict]:
    """Every supplier on the buyer's org watchlist(s), annotated with live status.

    Returns [] when the buyer has no org / empty watchlist (Starter single-seat is
    the common case) — the caller falls back to the tender section. Rows carry
    `resolved=False` when the supplier isn't a claimed marketplace profile yet.
    Ordered so alerting suppliers (FLAGGED/CRITICAL) surface first.
    """
    try:
        from app.core.models import VendorWatchlistItem

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
