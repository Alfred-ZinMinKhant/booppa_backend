"""Vendor Active / Vendor Pro insight builders.

Reusable, read-only, best-effort helpers that turn data the platform already
captures into the substance behind the monthly digest, the status-snapshot PDF,
and the dashboard:

  * `get_score_trend`        — Trust/Compliance deltas vs the previous snapshot.
  * `get_sector_benchmark`   — where the vendor sits vs its sector (percentile).
  * `get_trust_breakdown`    — per-dimension Trust Score + point-attribution.
  * `get_sector_rank`        — absolute rank (#N of M) among sector peers.
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


# Actionable Trust Score dimensions (Recency is excluded — it decays
# automatically and has no clear user action). Each entry: the VendorScore
# column, the human label, and the single recommended action that lifts it.
_TRUST_DIMENSIONS = [
    ("compliance_score", "COMPLIANCE", "Compliance",
     "Complete a PDPA Snapshot scan to raise your verified compliance score"),
    ("visibility_score", "VISIBILITY", "Visibility",
     "Add your company logo and description, and share your verify link"),
    ("engagement_score", "ENGAGEMENT", "Engagement",
     "Submit a bid response or upload a new proof in the next 30 days"),
    ("procurement_interest_score", "PROCUREMENT_INTEREST", "Procurement",
     "Keep your profile active and complete the PDPA Snapshot to attract buyer interest"),
]


def get_trust_breakdown(db, vendor_id: str) -> dict | None:
    """Per-dimension Trust Score breakdown with point-attribution.

    Returns:
      {
        "total": int,                      # current total trust score (/100)
        "projected_total": int,            # total after the top-3 actions
        "dimensions": [
          {"label", "score", "action", "potential_points"}, ...
        ],
        "top_actions": [ ...same shape..., highest impact first ],
      }
    `potential_points` is the contribution a dimension would add to the total
    if raised to 100, i.e. weight * (100 - score) using the live scoring
    weights. Returns None when the vendor has no VendorScore row.
    """
    try:
        from app.core.models_v6 import VendorScore
        from app.services.scoring import VendorScoreEngine

        rec = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        if not rec:
            return None

        weights = VendorScoreEngine.WEIGHTS
        dims = []
        for col, weight_key, label, action in _TRUST_DIMENSIONS:
            score = int(getattr(rec, col, 0) or 0)
            weight = float(weights.get(weight_key, 0.0))
            potential = int(round(weight * max(0, 100 - score)))
            dims.append({
                "label": label,
                "score": score,
                "action": action,
                "potential_points": potential,
            })

        top_actions = sorted(
            [d for d in dims if d["potential_points"] > 0],
            key=lambda d: d["potential_points"], reverse=True,
        )[:3]

        total = int(getattr(rec, "total_score", 0) or 0)
        projected = min(100, total + sum(d["potential_points"] for d in top_actions))
        return {
            "total": total,
            "projected_total": projected,
            "dimensions": dims,
            "top_actions": top_actions,
        }
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[VendorInsights] trust_breakdown failed for %s: %s", vendor_id, e)
        return None


def get_sector_rank(db, vendor_id: str) -> dict | None:
    """Vendor's absolute rank among sector peers by total Trust Score.

    Returns {"sector", "rank", "total"} where rank is 1-based (#1 = highest
    score). Used for the "Your position in [sector] vendor searches: #N of M"
    line. Returns None when the vendor has no sector tag or no peers.
    """
    try:
        from app.core.models_v6 import VendorScore, VendorSector

        sector_row = (
            db.query(VendorSector).filter(VendorSector.vendor_id == vendor_id).first()
        )
        sector = sector_row.sector if sector_row else None
        if not sector:
            return None

        # All vendors sharing this sector, ranked by total score (desc).
        peer_scores = (
            db.query(VendorScore.vendor_id, VendorScore.total_score)
            .join(VendorSector, VendorSector.vendor_id == VendorScore.vendor_id)
            .filter(VendorSector.sector == sector)
            .all()
        )
        if not peer_scores:
            return None

        ordered = sorted(
            peer_scores, key=lambda r: (r[1] if r[1] is not None else -1), reverse=True
        )
        total = len(ordered)
        rank = next(
            (i + 1 for i, r in enumerate(ordered) if str(r[0]) == str(vendor_id)),
            None,
        )
        if rank is None:
            return None
        return {"sector": sector, "rank": rank, "total": total}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[VendorInsights] sector_rank failed for %s: %s", vendor_id, e)
        return None


def get_search_impressions_30d(db, vendor_id: str) -> int | None:
    """Count of buyer-search appearances for this vendor in the trailing 30 days.

    Backs the Vendor Active "your profile appeared in N searches this month"
    line. Returns None on any failure / when the table isn't present yet, so the
    snapshot falls back to its honest placeholder note.
    """
    try:
        from datetime import timedelta
        from app.core.models_v10 import SearchImpression

        since = datetime.now(timezone.utc) - timedelta(days=30)
        return (
            db.query(SearchImpression)
            .filter(
                SearchImpression.vendor_id == vendor_id,
                SearchImpression.created_at >= since,
            )
            .count()
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[VendorInsights] search_impressions failed for %s: %s", vendor_id, e)
        return None


def get_tender_matches(db, vendor_id: str, limit: int = 5,
                       with_win_probability: bool = False) -> list[dict]:
    """Personalised BID/WATCH/PASS on the soonest-closing open GeBIZ tenders.

    Reuses the Tender Intelligence classifier (`build_vendor_history` +
    `enrich_tender_digest_with_classifications`). Falls back to an unclassified
    list (label None) when the vendor has no sector/history. Returns at most
    `limit` items, BID first. Empty list on any failure.

    When ``with_win_probability`` (Vendor Pro), each match is annotated with a
    ``win_probability`` percentage via `compute_tender_win_probability`.
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
        result = enriched[:limit]

        if with_win_probability:
            from app.services.tender_service import compute_tender_win_probability
            for m in result:
                try:
                    wp = compute_tender_win_probability(db, m.get("tender_no"), vendor_id)
                    if isinstance(wp, dict) and "currentProbability" in wp:
                        m["win_probability"] = wp["currentProbability"]
                except Exception as we:
                    logger.warning("[VendorInsights] win-prob failed for %s: %s", m.get("tender_no"), we)
        return result
    except Exception as e:  # pragma: no cover
        logger.warning("[VendorInsights] tender_matches failed for %s: %s", vendor_id, e)
        return []


def get_competitor_pulse(db, vendor_id: str) -> dict | None:
    """Sector competitor intelligence (top suppliers, win-rate by size, trend).

    Reuses `competitor_signals_generator`. Returns None when the vendor has no
    sector or no award data. Shape: the `_get_signals` dict.
    """
    try:
        from app.services.competitor_signals_generator import _sector_for_vendor, _get_signals

        sector = _sector_for_vendor(db, str(vendor_id))
        if not sector:
            return None
        signals = _get_signals(db, sector)
        if not signals or not signals.get("total_awards"):
            return None
        return signals
    except Exception as e:  # pragma: no cover
        logger.warning("[VendorInsights] competitor_pulse failed for %s: %s", vendor_id, e)
        return None


def get_pdpa_drift(db, vendor_id: str) -> dict | None:
    """PDPA posture + drift from the vendor's two most recent completed scans.

    Returns {current_score, previous_score, dimension_changes, scanned_url}
    suitable for `generate_pdpa_monitor_report_pdf`. None when no PDPA scans.
    """
    try:
        from app.core.models import Report
        from app.services.pdpa_dimension_snapshot import compute_dimension_snapshots, diff_snapshots

        reports = (
            db.query(Report)
            .filter(
                Report.owner_id == vendor_id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .limit(2)
            .all()
        )
        if not reports:
            return None
        cur = reports[0]
        prev = reports[1] if len(reports) > 1 else None

        def _score(r):
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            for k in ("overall_score", "compliance_score", "score"):
                if ad.get(k) is not None:
                    try:
                        return int(round(float(ad[k])))
                    except (TypeError, ValueError):
                        pass
            return None

        dimension_changes = []
        if prev:
            try:
                dimension_changes = diff_snapshots(
                    compute_dimension_snapshots(prev.assessment_data),
                    compute_dimension_snapshots(cur.assessment_data),
                )
            except Exception:
                dimension_changes = []

        return {
            "current_score": _score(cur),
            "previous_score": _score(prev) if prev else None,
            "dimension_changes": dimension_changes,
            "scanned_url": getattr(cur, "company_website", None),
        }
    except Exception as e:  # pragma: no cover
        logger.warning("[VendorInsights] pdpa_drift failed for %s: %s", vendor_id, e)
        return None
