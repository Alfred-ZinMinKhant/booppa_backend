"""Vendor Active / Vendor Pro insight builders.

Reusable, read-only, best-effort helpers that turn data the platform already
captures into the substance behind the monthly digest, the status-snapshot PDF,
and the dashboard:

  * `get_score_trend`        — Trust/Compliance deltas vs the previous snapshot.
  * `get_sector_benchmark`   — where the vendor sits vs its sector (percentile).
  * `get_tender_matches`     — personalised BID/WATCH/PASS on open GeBIZ tenders.

Every function swallows its own exceptions and returns None / [] so a single
data gap can never block an email or a PDF. Nothing here writes to the DB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def get_score_trend(db, vendor_id: str) -> dict | None:
    """Trust + Compliance + sector-percentile deltas vs the previous snapshot.

    Returns None when there is no snapshot history. Deltas are None on the very
    first cycle (only one snapshot exists).
    """
    try:
        from app.core.models_v8 import ScoreSnapshot

        rows = (
            db.query(ScoreSnapshot)
            .filter(ScoreSnapshot.vendor_id == vendor_id)
            .order_by(ScoreSnapshot.snapshot_at.desc())
            .limit(2)
            .all()
        )
        if not rows:
            return None
        cur = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def _comp(s) -> int | None:
            b = s.breakdown if isinstance(s.breakdown, dict) else None
            v = b.get("compliance") if b else None
            try:
                return int(round(float(v))) if v is not None else None
            except (TypeError, ValueError):
                return None

        cur_comp, prev_comp = _comp(cur), (_comp(prev) if prev else None)
        return {
            "total": cur.final_score,
            "total_delta": (cur.final_score - prev.final_score) if prev else None,
            "compliance": cur_comp,
            "compliance_delta": (cur_comp - prev_comp)
            if (prev and cur_comp is not None and prev_comp is not None) else None,
            "sector_percentile": round(float(cur.sector_percentile)) if cur.sector_percentile is not None else None,
        }
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[VendorInsights] score_trend failed for %s: %s", vendor_id, e)
        return None


def get_sector_benchmark(db, vendor_id: str) -> dict | None:
    """{'sector': str, 'percentile': int} — vendor's standing in its sector.

    Percentile comes from the latest ScoreSnapshot (50 = median). Returns None
    when the vendor has no sector tag or no snapshot.
    """
    try:
        from app.core.models_v6 import VendorSector
        from app.core.models_v8 import ScoreSnapshot

        sector_row = (
            db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).first()
        )
        sector = sector_row.sector if sector_row else None
        if not sector:
            return None
        snap = (
            db.query(ScoreSnapshot)
            .filter(ScoreSnapshot.vendor_id == vendor_id)
            .order_by(ScoreSnapshot.snapshot_at.desc())
            .first()
        )
        pct = round(float(snap.sector_percentile)) if (snap and snap.sector_percentile is not None) else None
        if pct is None:
            return None
        return {"sector": sector, "percentile": pct}
    except Exception as e:  # pragma: no cover
        logger.warning("[VendorInsights] sector_benchmark failed for %s: %s", vendor_id, e)
        return None


def get_tender_matches(db, vendor_id: str, limit: int = 5) -> list[dict]:
    """Personalised BID/WATCH/PASS on the soonest-closing open GeBIZ tenders.

    Reuses the Tender Intelligence classifier (`build_vendor_history` +
    `enrich_tender_digest_with_classifications`). Falls back to an unclassified
    list (label None) when the vendor has no sector/history. Returns at most
    `limit` items, BID first. Empty list on any failure.
    """
    try:
        from app.core.models_gebiz import GebizTender
        from app.core.models_v6 import VendorSector
        from app.services.tender_service_bid_classifier import (
            build_vendor_history,
            enrich_tender_digest_with_classifications,
        )

        now = datetime.now(timezone.utc)
        tenders = (
            db.query(GebizTender)
            .filter(GebizTender.status == "Open", GebizTender.closing_date >= now)
            .order_by(GebizTender.closing_date.asc())
            .limit(20)
            .all()
        )
        if not tenders:
            return []

        sector_row = db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).first()
        sector = sector_row.sector if sector_row else None

        rows = [
            {
                "tender_no": t.tender_no,
                "title": t.title,
                "agency": t.agency,
                "closing_date": t.closing_date,
                "estimated_value": t.estimated_value,
                "sector": (t.raw_data or {}).get("category") if isinstance(t.raw_data, dict) else None,
                "status": t.status,
                "url": t.url,
            }
            for t in tenders
        ]

        vendor_history = None
        if sector:
            try:
                vendor_history = build_vendor_history(db, str(vendor_id), sector=sector)
            except Exception as he:
                logger.warning("[VendorInsights] build_vendor_history failed for %s: %s", vendor_id, he)

        enriched = enrich_tender_digest_with_classifications(rows, vendor_history, max_classify=limit * 2)

        rank = {"BID": 0, "WATCH": 1, "PASS": 2, None: 3}
        enriched.sort(key=lambda r: (rank.get(r.get("bid_label"), 3), r.get("closing_date") or now))
        return enriched[:limit]
    except Exception as e:  # pragma: no cover
        logger.warning("[VendorInsights] tender_matches failed for %s: %s", vendor_id, e)
        return []
