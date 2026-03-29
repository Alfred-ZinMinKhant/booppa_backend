"""
Tender Win Probability Service
===============================
Computes estimated win probability for a GeBIZ tender given a vendor profile.

Formula:
    probability = base_rate * profile_mult * sector_mult * evidence_mult * risk_penalty

Multipliers are derived from VendorStatusSnapshot trust facts — never from
payment or plan state.

Projections simulate the vendor's profile after upgrading to RFP Express
or RFP Complete, showing the realistic delta achievable through each tier.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.core.models_v10 import TenderShortlist
from app.core.models_v8 import VendorStatusSnapshot

logger = logging.getLogger(__name__)

SNAPSHOT_STALE_DAYS = 7

# ── Probability cap ────────────────────────────────────────────────────────────
# Even a perfect profile can't exceed this ceiling (market reality)
MAX_PROBABILITY = 0.65

# ── Profile multiplier (verification depth) ───────────────────────────────────
PROFILE_MULT = {
    "UNVERIFIED": 0.50,
    "BASIC":      0.70,
    "STANDARD":   0.90,
    "DEEP":       1.10,
    "CERTIFIED":  1.30,
}

# ── Sector percentile multiplier ──────────────────────────────────────────────
def _sector_mult(percentile: float) -> float:
    if percentile >= 75:
        return 1.15
    elif percentile >= 50:
        return 1.00
    elif percentile >= 25:
        return 0.90
    else:
        return 0.80


# ── Evidence count multiplier ─────────────────────────────────────────────────
def _evidence_mult(evidence_count: int) -> float:
    if evidence_count >= 6:
        return 1.15
    elif evidence_count >= 3:
        return 1.05
    elif evidence_count >= 1:
        return 0.95
    else:
        return 0.80


# ── Risk penalty ──────────────────────────────────────────────────────────────
RISK_PENALTY = {
    "CLEAN":    1.00,
    "WATCH":    0.90,
    "FLAGGED":  0.70,
    "CRITICAL": 0.40,
}

# ── RFP product simulated profile upgrades ────────────────────────────────────
# Express: achieves DEEP verification + 3 evidence items
RFP_EXPRESS_DEPTH    = "DEEP"
RFP_EXPRESS_EVIDENCE = 3

# Complete: achieves CERTIFIED verification + 6 evidence items
RFP_COMPLETE_DEPTH    = "CERTIFIED"
RFP_COMPLETE_EVIDENCE = 6


def _compute_raw_probability(
    base_rate: float,
    verification_depth: str,
    sector_percentile: float,
    evidence_count: int,
    risk_signal: str,
) -> float:
    p_mult  = PROFILE_MULT.get(verification_depth, 0.50)
    s_mult  = _sector_mult(sector_percentile)
    e_mult  = _evidence_mult(evidence_count)
    r_pen   = RISK_PENALTY.get(risk_signal, 1.0)
    raw     = base_rate * p_mult * s_mult * e_mult * r_pen
    return min(raw, MAX_PROBABILITY)


def _build_gap_reasons(
    verification_depth: str,
    sector_percentile: float,
    evidence_count: int,
    risk_signal: str,
) -> list[str]:
    reasons: list[str] = []

    if verification_depth in ("UNVERIFIED", "BASIC"):
        reasons.append(
            f"Verification depth is {verification_depth} — agencies typically shortlist STANDARD or higher"
        )
    if sector_percentile < 50:
        reasons.append(
            f"Your sector percentile ({sector_percentile:.0f}th) is below the median — "
            "stronger trust signals are needed to compete"
        )
    if evidence_count < 3:
        reasons.append(
            f"Only {evidence_count} verified evidence item(s) on file — "
            "agencies expect at least 3 for serious consideration"
        )
    if risk_signal != "CLEAN":
        reasons.append(
            f"Open risk signal ({risk_signal}) detected — resolve anomalies to restore full credibility"
        )
    if not reasons:
        reasons.append(
            "Profile is competitive — continue maintaining evidence and monitoring cadence"
        )

    return reasons


def compute_tender_win_probability(
    db: Session,
    tender_no: str,
    vendor_id: Optional[str] = None,
) -> dict:
    """
    Compute the TenderWinProbabilityResult for a given tender + optional vendor.

    If vendor_id is None, returns a guest view with sector-median defaults
    and no projections.
    """
    tender = db.query(TenderShortlist).filter(
        TenderShortlist.tender_no == tender_no
    ).first()
    if not tender:
        return {"error": "tender_not_found", "tender_no": tender_no}

    # ── Vendor profile ────────────────────────────────────────────────────────
    snapshot: Optional[VendorStatusSnapshot] = None
    if vendor_id:
        snapshot = db.query(VendorStatusSnapshot).filter(
            VendorStatusSnapshot.vendor_id == vendor_id
        ).first()

        # 4.6: Auto-refresh stale snapshots before computing probability
        if snapshot and snapshot.updated_at:
            age = datetime.utcnow() - snapshot.updated_at
            if age > timedelta(days=SNAPSHOT_STALE_DAYS):
                try:
                    from app.services.scoring import VendorScoreEngine
                    VendorScoreEngine.update_vendor_score(db, vendor_id)
                    db.refresh(snapshot)
                    logger.info(
                        f"[TenderService] Refreshed stale snapshot for vendor={vendor_id} "
                        f"(age={age.days}d)"
                    )
                except Exception as e:
                    logger.warning(f"[TenderService] Snapshot refresh failed for {vendor_id}: {e}")

    # Fall back to unverified defaults when no snapshot exists
    verification_depth = snapshot.verification_depth if snapshot else "UNVERIFIED"
    sector_percentile  = snapshot.risk_adjusted_pct  if snapshot else 50.0
    evidence_count     = snapshot.evidence_count      if snapshot else 0
    risk_signal        = snapshot.risk_signal         if snapshot else "CLEAN"

    # ── Current probability ───────────────────────────────────────────────────
    current_prob = _compute_raw_probability(
        tender.base_rate,
        verification_depth,
        sector_percentile,
        evidence_count,
        risk_signal,
    )

    # ── Projections ───────────────────────────────────────────────────────────
    express_prob = _compute_raw_probability(
        tender.base_rate,
        max_depth(verification_depth, RFP_EXPRESS_DEPTH),
        sector_percentile,
        max(evidence_count, RFP_EXPRESS_EVIDENCE),
        risk_signal,
    )
    complete_prob = _compute_raw_probability(
        tender.base_rate,
        RFP_COMPLETE_DEPTH,
        sector_percentile,
        max(evidence_count, RFP_COMPLETE_EVIDENCE),
        risk_signal,
    )

    express_delta  = round((express_prob  - current_prob) * 100, 1)
    complete_delta = round((complete_prob - current_prob) * 100, 1)

    # ── Gap reasons ───────────────────────────────────────────────────────────
    gap_reasons = _build_gap_reasons(
        verification_depth, sector_percentile, evidence_count, risk_signal
    )

    # 4.7: Data freshness metadata
    data_freshness: dict | None = None
    if vendor_id and snapshot:
        snapshot_ts = snapshot.updated_at.isoformat() if snapshot.updated_at else None
        data_freshness = {
            "vendorSnapshot": snapshot_ts,
            "snapshotAgeDays": (
                (datetime.utcnow() - snapshot.updated_at).days
                if snapshot.updated_at else None
            ),
            "tenderData": tender.updated_at.isoformat() if getattr(tender, "updated_at", None) else None,
        }

    return {
        "tenderNo":          tender.tender_no,
        "tenderDescription": tender.description or "",
        "sector":            tender.sector,
        "agency":            tender.agency,
        "currentProbability": round(current_prob * 100, 1),
        "vendorProfile": {
            "verificationDepth": verification_depth,
            "evidenceCount":     evidence_count,
            "riskSignal":        risk_signal,
            "sectorPercentile":  sector_percentile,
        } if vendor_id else None,
        "projections": {
            "rfpExpress": {
                "probability": round(express_prob * 100, 1),
                "delta":       express_delta,
                "label":       "RFP Express",
                "simulates":   f"Achieves {RFP_EXPRESS_DEPTH} verification + {RFP_EXPRESS_EVIDENCE} evidence items",
            },
            "rfpComplete": {
                "probability": round(complete_prob * 100, 1),
                "delta":       complete_delta,
                "label":       "RFP Complete",
                "simulates":   f"Achieves {RFP_COMPLETE_DEPTH} verification + {RFP_COMPLETE_EVIDENCE} evidence items",
            },
        } if vendor_id else None,
        "gapReasons": gap_reasons if vendor_id else [],
        "dataFreshness": data_freshness,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────
_DEPTH_RANK = {
    "UNVERIFIED": 0,
    "BASIC":      1,
    "STANDARD":   2,
    "DEEP":       3,
    "CERTIFIED":  4,
}
_RANK_DEPTH = {v: k for k, v in _DEPTH_RANK.items()}


def max_depth(a: str, b: str) -> str:
    """Return the deeper of two verification depth strings."""
    return _RANK_DEPTH[max(_DEPTH_RANK.get(a, 0), _DEPTH_RANK.get(b, 0))]
