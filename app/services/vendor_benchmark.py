"""Vendor Proof — sector benchmark for the Trust Score.

A standalone Trust Score number ("42/100") is hard for a procurement officer to
weigh in isolation. This module turns it into a *relative* signal: how the vendor
compares against peers in the same ACRA sector.

Design (confirmed with product):
  - Primary basis is the **same-industry cohort** — Booppa-scanned vendors sharing
    the vendor's sector (`VendorSector.sector`).
  - When too few peers exist in that sector to be meaningful (`min_peers`), fall
    back to the **all-vendors** population so the benchmark is honest about its
    basis rather than reporting a percentile off 2 data points.
  - When even the all-vendors population is too thin, return None — the certificate
    then simply omits the benchmark line rather than inventing a comparison.

`benchmark_stats` is pure (no DB) so the percentile maths is unit-testable in
isolation; `compute_sector_benchmark` is the DB-querying wrapper used at the
Vendor Proof fulfillment call site.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# A sector needs at least this many scored peers (excluding the vendor itself)
# before we quote a sector-specific percentile; otherwise we fall back.
MIN_SECTOR_PEERS = 5
# Below this the all-vendors population is too thin to benchmark at all.
MIN_TOTAL_PEERS = 3


def benchmark_stats(
    my_score: int,
    peer_scores: List[int],
    sector: str,
    basis: str,
) -> Optional[Dict[str, Any]]:
    """Compute benchmark stats for one vendor against a peer score list.

    `peer_scores` must EXCLUDE the vendor's own score. Returns a dict:
      {sector, basis, peer_count, sector_avg, percentile}
    where `percentile` is the share of peers the vendor scores at-or-above
    (0-100), or None when there are too few peers to say anything.
    """
    scores = [int(s) for s in peer_scores if s is not None]
    if len(scores) < MIN_TOTAL_PEERS:
        return None
    at_or_below = sum(1 for s in scores if my_score >= s)
    percentile = round(at_or_below / len(scores) * 100)
    avg = round(sum(scores) / len(scores))
    return {
        "sector": sector,
        "basis": basis,
        "peer_count": len(scores),
        "sector_avg": avg,
        "percentile": percentile,
    }


def compute_sector_benchmark(
    db,
    vendor_id,
    my_score: Optional[int],
    sector: Optional[str],
    min_peers: int = MIN_SECTOR_PEERS,
) -> Optional[Dict[str, Any]]:
    """DB wrapper: benchmark the vendor's Trust Score against its sector cohort.

    Best-effort — any failure returns None so certificate generation is never
    blocked by a benchmark lookup. Excludes the vendor's own row from the peer
    population so it never benchmarks against itself.
    """
    if my_score is None:
        return None
    try:
        from app.core.models import VendorScore, VendorSector

        def _scores_for_sector(sec: str) -> List[int]:
            rows = (
                db.query(VendorScore.total_score)
                .join(VendorSector, VendorScore.vendor_id == VendorSector.vendor_id)
                .filter(
                    VendorSector.sector == sec,
                    VendorScore.vendor_id != vendor_id,
                    VendorScore.total_score.isnot(None),
                )
                .all()
            )
            return [r[0] for r in rows if r[0] is not None]

        # Same-industry cohort first.
        if sector:
            peers = _scores_for_sector(sector)
            if len(peers) >= min_peers:
                return benchmark_stats(int(my_score), peers, sector, basis="sector")

        # Fall back to the all-vendors population (labelled honestly).
        all_rows = (
            db.query(VendorScore.total_score)
            .filter(
                VendorScore.vendor_id != vendor_id,
                VendorScore.total_score.isnot(None),
            )
            .all()
        )
        all_peers = [r[0] for r in all_rows if r[0] is not None]
        label = sector or "all sectors"
        return benchmark_stats(int(my_score), all_peers, label, basis="all_vendors")
    except Exception:  # pragma: no cover - benchmark is best-effort
        logger.exception("[VendorBenchmark] sector benchmark computation failed")
        return None
