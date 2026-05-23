"""
Tender similarity scoring for the Vendor Pro competitor-awareness signal.

Given a focal tender, return up to N other tenders that share sector and
(optionally) agency or amount-band. The signal is conservative on purpose
— a single shared dimension can be noisy, so we require sector match by
default and rank by additional dimensions matched.
"""

from typing import Optional
from sqlalchemy.orm import Session

from app.core.models_v10 import TenderShortlist
from app.core.models_gebiz import GebizTender


def _amount_band(amt: Optional[float]) -> str:
    """Mirror of tender_intelligence._classify_amount_band — duplicated here
    rather than imported to avoid a circular import (tender_intelligence imports
    this module). Keep the bands in lockstep manually."""
    if amt is None:
        return "unknown"
    if amt < 50_000:
        return "<50k"
    if amt < 250_000:
        return "50k-250k"
    if amt < 1_000_000:
        return "250k-1M"
    if amt < 5_000_000:
        return "1M-5M"
    return "5M+"


def find_similar_tenders(
    db: Session,
    tender_no: str,
    limit: int = 10,
) -> list[str]:
    """Return a list of tender_no strings similar to `tender_no`, ranked by
    sector + agency + amount-band match count.

    Uses TenderShortlist + GebizTender as the candidate pool. Excludes the
    focal tender itself. Returns at most `limit` matches.
    """
    # Resolve focal sector/agency/amount.
    focal = db.query(TenderShortlist).filter(TenderShortlist.tender_no == tender_no).first()
    focal_sector: Optional[str] = focal.sector if focal else None
    focal_agency: Optional[str] = focal.agency if focal else None
    focal_amount: Optional[float] = None
    if not focal_sector:
        # Fall back to the live GeBIZ feed if we have no shortlist row.
        gebiz = db.query(GebizTender).filter(GebizTender.tender_no == tender_no).first()
        if gebiz:
            focal_agency = focal_agency or gebiz.agency
            focal_amount = gebiz.estimated_value
            # GebizTender has no sector — leave as None and rely on agency match
    if not focal_sector and not focal_agency:
        return []

    focal_band = _amount_band(focal_amount)

    # Candidate pool: TenderShortlist rows in the same sector OR same agency.
    q = db.query(TenderShortlist).filter(TenderShortlist.tender_no != tender_no)
    if focal_sector:
        q = q.filter(TenderShortlist.sector == focal_sector)
    elif focal_agency:
        q = q.filter(TenderShortlist.agency == focal_agency)
    candidates = q.limit(500).all()

    # Score each candidate: sector match (1) + agency match (1) + amount band match (1).
    scored: list[tuple[int, str]] = []
    for c in candidates:
        score = 0
        if focal_sector and c.sector == focal_sector:
            score += 1
        if focal_agency and c.agency == focal_agency:
            score += 1
        # Amount band — only meaningful if we have a focal amount.
        if focal_band != "unknown":
            # Look up live GeBIZ amount for the candidate; skip if missing.
            cand_gebiz = (
                db.query(GebizTender)
                .filter(GebizTender.tender_no == c.tender_no)
                .first()
            )
            if cand_gebiz and _amount_band(cand_gebiz.estimated_value) == focal_band:
                score += 1
        if score >= 1:
            scored.append((score, c.tender_no))

    # Highest score first, then alphabetical for deterministic ordering.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [tno for _, tno in scored[:limit]]
