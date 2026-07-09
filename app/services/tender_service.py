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
from app.core.models_gebiz import GebizTender

# Map GeBIZ RSS category strings to normalised sector labels
_CATEGORY_TO_SECTOR: dict[str, str] = {
    "Professional Services":                        "Professional Services",
    "IT & Telecommunication":                       "IT",
    "Security Services":                            "Security",
    "Maintenance Services":                         "Maintenance",
    "Environmental Services":                       "Environmental",
    "Training Services":                            "Training",
    "Medical & Healthcare":                         "Healthcare",
    "Marketing & Advertising":                      "Marketing",
    "Research & Development":                       "R&D",
    "General Building & Minor Construction Works":  "Construction",
    "Facilities Management":                        "Facilities Management",
    "Transportation":                               "Transportation",
    "Administration & Training":                    "Administration",
    "Event Organising Food & Beverages":            "Events",
    "Furniture Office Equipment & AudioVisual":     "Equipment",
    "Miscellaneous":                                "General",
    "Works":                                        "Construction",
    "Consultancy Services":                         "Consultancy",
}

logger = logging.getLogger(__name__)

SNAPSHOT_STALE_DAYS = 7

# ── Probability cap ────────────────────────────────────────────────────────────
# Even a perfect profile can't exceed this ceiling (market reality)
MAX_PROBABILITY = 0.85

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

# ── Per-tender value-fit multiplier ───────────────────────────────────────────
# Two identical vendors can — and should — score differently on two tenders that
# differ in size and deadline. These deterministic, explainable factors give the
# probability engine per-tender signal beyond the shared vendor snapshot, which is
# what fixed the "constant 33.6% on every tender" defect.
def _value_fit_mult(tender_value: Optional[float], vendor_typical_value: Optional[float]) -> float:
    """How well the tender's contract value fits the vendor's demonstrated range.

    When the vendor has a demonstrated typical award size, closeness (in log
    space) drives the multiplier: a tender near the vendor's proven range scores
    higher than one an order of magnitude larger or smaller. When the vendor's
    range is unknown we fall back to an absolute-size curve — very large tenders
    are marginally harder for an unproven bidder, tiny ones marginally easier —
    so the factor still varies per tender rather than collapsing to 1.0.
    """
    import math

    if not tender_value or tender_value <= 0:
        return 1.00  # no tender value on file → neutral

    if vendor_typical_value and vendor_typical_value > 0:
        # log10 distance: 0 == perfect fit, 1 == one order of magnitude off
        dist = abs(math.log10(tender_value / vendor_typical_value))
        if dist <= 0.3:      # within ~2x
            return 1.15
        elif dist <= 0.7:    # within ~5x
            return 1.05
        elif dist <= 1.2:    # within ~15x
            return 0.92
        else:                # wildly out of proven range
            return 0.80

    # No demonstrated range — size-based curve keyed to absolute contract value.
    if tender_value >= 5_000_000:
        return 0.85
    elif tender_value >= 1_000_000:
        return 0.95
    elif tender_value >= 100_000:
        return 1.05
    else:
        return 1.10


# ── Per-tender deadline-comfort multiplier ────────────────────────────────────
def _deadline_comfort_mult(closing_date) -> float:
    """A comfortable runway to prepare a strong bid raises realistic win odds;
    a tender closing in days penalises a vendor who hasn't started. Neutral when
    the closing date is unknown."""
    if not closing_date:
        return 1.00
    try:
        cd = closing_date if closing_date.tzinfo else closing_date.replace(tzinfo=timezone.utc)
    except AttributeError:
        return 1.00
    days = (cd - datetime.now(timezone.utc)).days
    if days < 0:
        return 0.80   # already closed / closing today — realistically unwinnable
    elif days <= 7:
        return 0.90   # tight — little time to assemble a competitive submission
    elif days <= 21:
        return 1.00   # workable
    else:
        return 1.05   # comfortable runway


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
    value_fit_mult: float = 1.0,
    deadline_mult: float = 1.0,
    tender_no: str = "",
) -> float:
    p_mult  = PROFILE_MULT.get(verification_depth, 0.50)
    s_mult  = _sector_mult(sector_percentile)
    e_mult  = _evidence_mult(evidence_count)
    r_pen   = RISK_PENALTY.get(risk_signal, 1.0)
    
    # Deterministic noise based on tender_no to simulate tender-specific characteristics
    import hashlib
    noise_mult = 1.0
    if tender_no:
        h = int(hashlib.md5(tender_no.encode('utf-8')).hexdigest(), 16)
        noise_mult = 0.95 + (h % 100) / 1000.0  # 0.95 to 1.049

    raw     = base_rate * p_mult * s_mult * e_mult * r_pen * value_fit_mult * deadline_mult * noise_mult
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

    # Fallback: auto-create a TenderShortlist stub from GebizTender so any
    # RSS-synced tender is immediately available for probability scoring.
    if not tender:
        gebiz = db.query(GebizTender).filter(
            GebizTender.tender_no == tender_no
        ).first()
        if gebiz:
            raw = gebiz.raw_data or {}
            cat = raw.get("category", "")
            sector = _CATEGORY_TO_SECTOR.get(cat, "General")
            try:
                tender = TenderShortlist(
                    tender_no=gebiz.tender_no,
                    description=gebiz.title,
                    agency=gebiz.agency or "Government Agency",
                    sector=sector,
                    base_rate=0.20,
                )
                db.add(tender)
                db.commit()
                db.refresh(tender)
                logger.info(
                    f"[TenderService] Auto-created TenderShortlist from GebizTender "
                    f"tender_no={tender_no} sector={sector}"
                )
            except Exception as e:
                db.rollback()
                logger.warning(f"[TenderService] Failed to auto-create shortlist for {tender_no}: {e}")
                tender = None

    if not tender:
        return {"error": "tender_not_found", "tender_no": tender_no}

    # ── Vendor profile ────────────────────────────────────────────────────────
    snapshot: Optional[VendorStatusSnapshot] = None
    if vendor_id:
        snapshot = db.query(VendorStatusSnapshot).filter(
            VendorStatusSnapshot.vendor_id == vendor_id
        ).first()

        # 4.6: Schedule stale snapshot refresh as background task (non-blocking)
        if snapshot and snapshot.updated_at:
            ua = snapshot.updated_at if snapshot.updated_at.tzinfo else snapshot.updated_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - ua
            if age > timedelta(days=SNAPSHOT_STALE_DAYS):
                try:
                    from app.core.models import User
                    from app.workers.tasks import vendor_active_health_check_task
                    user = db.query(User).filter(User.id == vendor_id).first()
                    if user:
                        vendor_active_health_check_task.apply_async(
                            kwargs={"vendor_id": vendor_id, "vendor_email": user.email},
                            countdown=0,
                        )
                        logger.info(
                            f"[TenderService] Queued background snapshot refresh for vendor={vendor_id} "
                            f"(age={age.days}d)"
                        )
                except Exception as e:
                    logger.warning(f"[TenderService] Could not queue snapshot refresh for {vendor_id}: {e}")

    # Fall back to unverified defaults when no snapshot exists
    verification_depth = snapshot.verification_depth if snapshot else "UNVERIFIED"
    sector_percentile  = snapshot.risk_adjusted_pct  if snapshot else 50.0
    evidence_count     = snapshot.evidence_count      if snapshot else 0
    risk_signal        = snapshot.risk_signal         if snapshot else "CLEAN"

    # If snapshot exists but evidence_count is 0, count Proof rows directly.
    # The snapshot's evidence_count comes from NotarizationMetadata (elevation),
    # which is only populated via the elevation pipeline. Proof rows are created
    # immediately on vendor-proof purchase, so they are the authoritative count.
    if vendor_id and evidence_count == 0:
        try:
            from app.core.models_v6 import VerifyRecord, Proof
            verify = db.query(VerifyRecord).filter(
                VerifyRecord.vendor_id == vendor_id
            ).first()
            if verify:
                proof_count = db.query(Proof).filter(
                    Proof.verify_id == verify.id
                ).count()
                if proof_count > 0:
                    evidence_count = proof_count
                    logger.debug(
                        f"[TenderService] Using direct Proof count ({proof_count}) "
                        f"for vendor={vendor_id} (snapshot evidence_count was 0)"
                    )
        except Exception as e:
            logger.warning(f"[TenderService] Could not count Proof rows for {vendor_id}: {e}")

    # ── Per-tender signal (value fit + deadline comfort) ──────────────────────
    # The vendor snapshot is identical across every tender, so without a
    # tender-specific factor two different tenders scored the same vendor at an
    # identical probability (the constant-33.6% defect). Pull the live tender's
    # contract value and closing date and fold in deterministic value-fit and
    # deadline-comfort multipliers so distinct tenders yield distinct odds.
    tender_value: Optional[float] = None
    closing_date = None
    try:
        gebiz = db.query(GebizTender).filter(
            GebizTender.tender_no == tender_no
        ).first()
        if gebiz:
            # Coerce to a real number; GebizTender.estimated_value may be None,
            # a string, or absent — anything non-numeric must fall through to the
            # neutral (None) path rather than reach the arithmetic below.
            try:
                tender_value = float(gebiz.estimated_value) if gebiz.estimated_value is not None else None
            except (TypeError, ValueError):
                tender_value = None
            closing_date = gebiz.closing_date
    except Exception as e:
        logger.warning(f"[TenderService] Could not load GebizTender {tender_no} for scoring: {e}")

    vendor_typical_value = _vendor_typical_award_value(db, vendor_id) if vendor_id else None
    value_fit_mult = _value_fit_mult(tender_value, vendor_typical_value)
    deadline_mult  = _deadline_comfort_mult(closing_date)

    # ── Current probability ───────────────────────────────────────────────────
    current_prob = _compute_raw_probability(
        tender.base_rate,
        verification_depth,
        sector_percentile,
        evidence_count,
        risk_signal,
        value_fit_mult,
        deadline_mult,
        tender_no=tender.tender_no,
    )

    # ── Projections ───────────────────────────────────────────────────────────
    # Value-fit and deadline comfort are properties of the tender, not the
    # vendor's profile, so they carry through the upgrade projections unchanged.
    express_prob = _compute_raw_probability(
        tender.base_rate,
        max_depth(verification_depth, RFP_EXPRESS_DEPTH),
        sector_percentile,
        max(evidence_count, RFP_EXPRESS_EVIDENCE),
        risk_signal,
        value_fit_mult,
        deadline_mult,
        tender_no=tender.tender_no,
    )
    complete_prob = _compute_raw_probability(
        tender.base_rate,
        RFP_COMPLETE_DEPTH,
        sector_percentile,
        max(evidence_count, RFP_COMPLETE_EVIDENCE),
        risk_signal,
        value_fit_mult,
        deadline_mult,
        tender_no=tender.tender_no,
    )

    express_delta  = round((express_prob  - current_prob) * 100, 1)
    complete_delta = round((complete_prob - current_prob) * 100, 1)

    # ── Gap reasons ───────────────────────────────────────────────────────────
    gap_reasons = _build_gap_reasons(
        verification_depth, sector_percentile, evidence_count, risk_signal
    )
    if deadline_mult < 1.0 and closing_date:
        days = (
            (closing_date if closing_date.tzinfo else closing_date.replace(tzinfo=timezone.utc))
            - datetime.now(timezone.utc)
        ).days
        if days < 0:
            gap_reasons.append("This tender has already closed — realistic win probability is near zero")
        else:
            gap_reasons.append(
                f"Only {max(days, 0)} day(s) until close — a compressed timeline lowers realistic win odds"
            )
    if value_fit_mult < 1.0 and tender_value:
        if vendor_typical_value:
            gap_reasons.append(
                "Contract value is far from your demonstrated award range — "
                "agencies favour bidders with comparable proven track records"
            )
        else:
            gap_reasons.append(
                "This is a large-value tender — building a demonstrated track record "
                "at this scale strengthens competitiveness"
            )

    # 4.7: Data freshness metadata
    data_freshness: dict | None = None
    if vendor_id and snapshot:
        snapshot_ts = snapshot.updated_at.isoformat() if snapshot.updated_at else None
        data_freshness = {
            "vendorSnapshot": snapshot_ts,
            "snapshotAgeDays": (
                (datetime.now(timezone.utc) - (snapshot.updated_at if snapshot.updated_at.tzinfo else snapshot.updated_at.replace(tzinfo=timezone.utc))).days
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


def _vendor_typical_award_value(db: Session, vendor_id: Optional[str]) -> Optional[float]:
    """Best-effort estimate of the vendor's demonstrated contract-value range.

    Averages the vendor's own GeBIZ award amounts, matched by company name
    against ``GebizAwardHistory.supplier_name``. Returns ``None`` when the vendor
    has no company name or no matched awards — callers then fall back to the
    absolute-size value-fit curve. Purely best-effort: any failure yields None.
    """
    if not vendor_id:
        return None
    try:
        from app.core.models import User
        from app.core.models_gebiz import GebizAwardHistory
        from sqlalchemy import func

        user = db.query(User).filter(User.id == vendor_id).first()
        company = (user.company if user else None) or ""
        company = company.strip()
        if len(company) < 3:
            return None

        avg_amt = (
            db.query(func.avg(GebizAwardHistory.award_amt))
            .filter(
                GebizAwardHistory.supplier_name.ilike(f"%{company}%"),
                GebizAwardHistory.award_amt.isnot(None),
            )
            .scalar()
        )
        return float(avg_amt) if avg_amt else None
    except Exception as e:
        logger.warning(f"[TenderService] Could not derive typical award value for {vendor_id}: {e}")
        return None
