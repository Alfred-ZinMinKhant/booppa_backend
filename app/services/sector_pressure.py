"""
Sector Competitive Pressure Service — V8
=========================================
Provides a vendor's competitive snapshot within their primary sector,
based solely on elevation data from NotarizationMetadata.

Read-only — no writes to any table.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.core.models_v8 import NotarizationMetadata
from app.core.models import VendorSector

logger = logging.getLogger(__name__)

# In-memory sector cache: sector → {vendors: list, cached_at: datetime}
_SECTOR_CACHE: dict = {}
_CACHE_TTL_MINUTES = 5


def get_sector_competitive_pressure(db: Session, sector: str, vendor_id: str) -> dict:
    """
    Returns the competitive pressure snapshot for a vendor in their sector.

    Fields:
      vendorElevated       — bool: is this vendor ELEVATED in this sector?
      totalElevated        — int: total ELEVATED vendors in sector
      totalInSector        — int: total vendors in sector
      vendorEvidence       — int: this vendor's sector evidence count
      avgEvidence          — float: avg evidence of ELEVATED vendors in sector
      peerCount            — int: ELEVATED peers (excluding self)
      elevationRate        — float: % of sector that is ELEVATED
      vendorRank           — int | None: rank among ELEVATED (by confidence)
      competitorElevated   — bool: any ELEVATED competitor above this vendor?
    """
    cache_key = sector
    now = datetime.now(timezone.utc)

    # Check cache
    if cache_key in _SECTOR_CACHE:
        entry = _SECTOR_CACHE[cache_key]
        if (now - entry["cached_at"]).total_seconds() < _CACHE_TTL_MINUTES * 60:
            rows = entry["rows"]
        else:
            rows = _fetch_sector_rows(db, sector)
            _SECTOR_CACHE[cache_key] = {"rows": rows, "cached_at": now}
    else:
        rows = _fetch_sector_rows(db, sector)
        _SECTOR_CACHE[cache_key] = {"rows": rows, "cached_at": now}

    # Total vendors in sector
    total_in_sector = db.query(VendorSector).filter(
        VendorSector.sector == sector
    ).count()

    elevated_rows = [r for r in rows if r["structural_level"] == "ELEVATED"]
    peer_rows     = [r for r in elevated_rows if str(r["vendor_id"]) != str(vendor_id)]

    # This vendor's data
    vendor_row = next((r for r in rows if str(r["vendor_id"]) == str(vendor_id)), None)
    vendor_elevated  = vendor_row is not None and vendor_row["structural_level"] == "ELEVATED"
    vendor_evidence  = vendor_row["evidence_count"] if vendor_row else 0
    vendor_conf      = vendor_row["confidence_score"] if vendor_row else 0.0

    avg_evidence = (
        sum(r["evidence_count"] for r in elevated_rows) / len(elevated_rows)
        if elevated_rows else 0.0
    )

    elevation_rate = (
        round(len(elevated_rows) / total_in_sector * 100, 1)
        if total_in_sector > 0 else 0.0
    )

    # Rank this vendor among ELEVATED (sorted by confidence desc)
    sorted_elevated = sorted(elevated_rows, key=lambda r: r["confidence_score"], reverse=True)
    vendor_rank = None
    if vendor_elevated:
        for i, r in enumerate(sorted_elevated):
            if str(r["vendor_id"]) == str(vendor_id):
                vendor_rank = i + 1
                break

    # Competitor elevated above this vendor?
    competitor_elevated = any(
        r["confidence_score"] > vendor_conf for r in peer_rows
    )

    return {
        "sector":               sector,
        "vendorElevated":       vendor_elevated,
        "totalElevated":        len(elevated_rows),
        "totalInSector":        total_in_sector,
        "vendorEvidence":       vendor_evidence,
        "avgEvidence":          round(avg_evidence, 1),
        "peerCount":            len(peer_rows),
        "elevationRate":        elevation_rate,
        "vendorRank":           vendor_rank,
        "competitorElevated":   competitor_elevated,
    }


def _fetch_sector_rows(db: Session, sector: str) -> list:
    """DB query: NotarizationMetadata for all vendors in this sector."""
    vendor_ids_in_sector = db.query(VendorSector.vendor_id).filter(
        VendorSector.sector == sector
    ).all()
    ids = [str(v[0]) for v in vendor_ids_in_sector]

    if not ids:
        return []

    meta_rows = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id.in_(ids)
    ).all()

    result = []
    meta_by_vendor = {str(r.vendor_id): r for r in meta_rows}

    for vid in ids:
        row = meta_by_vendor.get(vid)
        if row:
            by_sector = row.evidence_count_by_sector or {}
            ev = by_sector.get(sector, by_sector.get("__all__", row.evidence_count))
            result.append({
                "vendor_id":       vid,
                "structural_level": row.structural_level,
                "verification_depth": row.verification_depth,
                "confidence_score":  row.confidence_score,
                "evidence_count":    ev,
                "notarized_at":      row.notarized_at,
            })
        else:
            result.append({
                "vendor_id":        vid,
                "structural_level": "STANDARD",
                "verification_depth": None,
                "confidence_score":  0.0,
                "evidence_count":    0,
                "notarized_at":      None,
            })

    return result


def get_cached_rows(sector: str) -> list:
    """Return cached sector rows (warm after get_sector_competitive_pressure call)."""
    entry = _SECTOR_CACHE.get(sector)
    return entry["rows"] if entry else []


def count_recently_active(rows: list, days: int = 30) -> int:
    """Pure function: count rows with notarized_at within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return sum(
        1 for r in rows
        if r.get("notarized_at") and r["notarized_at"] >= cutoff
    )


def generate_sector_pressure_message(snapshot: dict, recently_active_count: int) -> str:
    """
    Pure function: human-readable pressure message for the vendor dashboard.
    Non-aggressive, informational framing only.
    """
    total_elevated  = snapshot["totalElevated"]
    total_in_sector = snapshot["totalInSector"]
    is_elevated     = snapshot["vendorElevated"]
    vendor_rank     = snapshot["vendorRank"]
    peer_count      = snapshot["peerCount"]
    elevation_rate  = snapshot["elevationRate"]
    competitor_elev = snapshot["competitorElevated"]

    if total_in_sector == 0:
        return "No vendors found in your sector yet."

    if is_elevated and vendor_rank == 1:
        return (
            f"You are the top-ranked ELEVATED vendor in your sector "
            f"({total_elevated} elevated out of {total_in_sector} total). "
            f"Keep your evidence current to maintain your position."
        )

    if is_elevated:
        return (
            f"You are ranked #{vendor_rank} among {total_elevated} ELEVATED vendors "
            f"in your sector. {recently_active_count} vendors were active in the last 30 days."
        )

    if competitor_elev:
        return (
            f"There are {total_elevated} ELEVATED vendors in your sector "
            f"({elevation_rate}% of {total_in_sector}). "
            f"Completing a notarization would improve your visibility in procurement searches."
        )

    if total_elevated == 0:
        return (
            f"No vendors in your sector have completed notarization yet. "
            f"Be the first ELEVATED vendor in {snapshot['sector']} to stand out in procurement results."
        )

    return (
        f"Your sector has {total_in_sector} vendors. {total_elevated} have earned ELEVATED status. "
        f"Adding notarized evidence can strengthen your procurement profile."
    )
