"""
tender_service_bid_classifier.py — AI Bid/Watch/Pass classification

Adds rule-based BID / WATCH / PASS signals to the Tender Intelligence digest.
"Rule-based" is intentional: deterministic rules are fully auditable, zero
extra API cost, and produce consistent output. DeepSeek can be added later
for qualitative recommendation text if needed.

Usage: import classify_tender and call it for each live GeBIZ tender.
The result is a dict: {"label": "BID"|"WATCH"|"PASS", "reason": str, "confidence": int}

This module is drop-in added to the existing tender_service.py or imported
alongside it. No changes to existing tender_service functions are required.
"""
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)


def classify_tender(tender: dict, vendor_history: dict | None = None) -> dict:
    """
    Rule-based BID / WATCH / PASS classification for a single GeBIZ tender.

    Args:
        tender: Dict with keys from GebizTender model:
            - closing_date (datetime or None)
            - estimated_value (float or None)
            - title (str)
            - agency (str)
            - sector (str or None)
            - status (str)
        vendor_history: Optional dict with vendor-specific context:
            - sector_win_rate (float, 0.0–1.0): vendor's historical win rate in this sector
            - agency_win_rate (float, 0.0–1.0): vendor's win rate with this agency
            - avg_bid_size (float): vendor's average contract size
            - open_bids (int): currently active bids (workload pressure)

    Returns:
        {"label": "BID"|"WATCH"|"PASS", "reason": str, "confidence": int (0-100)}
    """
    now = datetime.now(timezone.utc)
    h = vendor_history or {}

    # ── Extract tender fields ─────────────────────────────────────────────────
    closing_dt = tender.get("closing_date")
    if closing_dt and closing_dt.tzinfo is None:
        # Naive datetime from DB — assume UTC
        closing_dt = closing_dt.replace(tzinfo=timezone.utc)

    days_to_close = (closing_dt - now).days if closing_dt else 999
    estimated_value = float(tender.get("estimated_value") or 0)
    agency = (tender.get("agency") or "").upper()
    sector = (tender.get("sector") or "").upper()
    title_lower = (tender.get("title") or "").lower()
    status = (tender.get("status") or "").lower()

    if status not in ("open", ""):
        return {
            "label": "PASS",
            "reason": f"Tender status is '{status}' — not open for submission.",
            "confidence": 95,
        }

    # ── PASS rules (hard stops) ───────────────────────────────────────────────

    # Too close to deadline to prepare a quality bid
    if days_to_close < 5:
        return {
            "label": "PASS",
            "reason": f"Closes in {days_to_close} day(s) — insufficient prep time for a quality bid.",
            "confidence": 90,
        }

    # Contract too large for vendor (if we know their avg bid size)
    avg_bid = float(h.get("avg_bid_size") or 0)
    if avg_bid > 0 and estimated_value > avg_bid * 5:
        return {
            "label": "PASS",
            "reason": (
                f"Contract size S${estimated_value:,.0f} is 5× your average bid "
                f"(S${avg_bid:,.0f}). Resource risk is high."
            ),
            "confidence": 80,
        }

    # Vendor overloaded with open bids
    open_bids = int(h.get("open_bids") or 0)
    if open_bids >= 5:
        return {
            "label": "WATCH",
            "reason": (
                f"You have {open_bids} open bids. Monitor this tender but "
                "avoid overcommitting until one closes."
            ),
            "confidence": 75,
        }

    # ── Compute composite score for BID vs WATCH ──────────────────────────────
    # Max 100 points. BID threshold: ≥50. WATCH: 30–49. PASS: <30.
    # Thresholds are calibrated for HONEST sector-level signals (no fabricated
    # per-vendor win rates — see build_vendor_history): the size-fit and deadline
    # factors carry real weight, so a strong sector fit can reach BID without
    # relying on an agency-relationship signal we don't actually have.

    score = 0
    reasons: list[str] = []

    # Factor 1: Sector fit (max 30pts). "Sector award rate" is a sector-level
    # estimate, NOT a verified per-vendor win rate — worded as such.
    sector_win_rate = float(h.get("sector_win_rate") or 0.15)
    if sector_win_rate >= 0.30:
        score += 30
        reasons.append(f"Strong sector fit (sector award rate ~{sector_win_rate:.0%}, est.)")
    elif sector_win_rate >= 0.15:
        score += 18
        reasons.append(f"Moderate sector fit (sector award rate ~{sector_win_rate:.0%}, est.)")
    elif sector_win_rate > 0:
        score += 8
        reasons.append(f"Weak sector fit (sector award rate ~{sector_win_rate:.0%}, est.)")

    # Factor 2: Agency relationship (max 20pts). Only contributes when a real
    # agency signal exists (default 0.0 → no points; honestly "no relationship").
    agency_win_rate = float(h.get("agency_win_rate") or 0)
    if agency_win_rate >= 0.25:
        score += 20
        reasons.append(f"Strong agency relationship ({agency}: {agency_win_rate:.0%})")
    elif agency_win_rate >= 0.10:
        score += 10
        reasons.append(f"Prior experience with {agency}")

    # Factor 3: Deadline comfort (max 20pts)
    if days_to_close >= 21:
        score += 20
        reasons.append(f"Comfortable deadline ({days_to_close}d remaining)")
    elif days_to_close >= 10:
        score += 12
        reasons.append(f"Adequate deadline ({days_to_close}d remaining)")
    else:
        score += 4
        reasons.append(f"Tight deadline ({days_to_close}d remaining)")

    # Factor 4: Contract size sweet spot (max 15pts)
    if avg_bid > 0:
        ratio = estimated_value / avg_bid if avg_bid else 1
        if 0.5 <= ratio <= 2.0:
            score += 15
            reasons.append(f"Contract size S${estimated_value:,.0f} fits your typical range")
        elif 0.3 <= ratio <= 3.0:
            score += 8

    # Factor 5: SME-friendly keywords (max 10pts) — common in Gebiz SME tenders
    sme_keywords = [
        "sme", "small medium", "local enterprise", "startup",
        "innovation", "agile", "pilot", "proof of concept"
    ]
    if any(k in title_lower for k in sme_keywords):
        score += 10
        reasons.append("SME-friendly tender language detected")

    # Factor 6: High-competition penalty (max -5pts)
    high_competition_agencies = {"MOH", "MOE", "LTA", "HDB", "EDB"}  # typically many large bidders
    if agency in high_competition_agencies and estimated_value > 500_000:
        score -= 5
        reasons.append(f"{agency} large contracts are highly competitive")

    # ── Final classification ──────────────────────────────────────────────────
    if score >= 50:
        label = "BID"
        primary_reason = (
            f"Score {score}/100 — " + "; ".join(reasons[:2]) + "."
            if reasons else f"Score {score}/100 — strong overall fit."
        )
        confidence = min(95, 60 + score // 3)
    elif score >= 30:
        label = "WATCH"
        primary_reason = (
            f"Score {score}/100 — " + "; ".join(reasons[:2]) + ". "
            "Monitor for scope changes before committing."
            if reasons else f"Score {score}/100 — marginal fit. Watch for updates."
        )
        confidence = min(80, 50 + score // 4)
    else:
        label = "PASS"
        primary_reason = (
            f"Score {score}/100 — insufficient fit for current resources."
        )
        confidence = min(80, 70 - score // 4)

    return {"label": label, "reason": primary_reason, "confidence": confidence}


def enrich_tender_digest_with_classifications(
    tenders: list[dict],
    vendor_history: dict | None = None,
    max_classify: int = 10,
) -> list[dict]:
    """
    Add 'bid_label', 'bid_reason', 'bid_confidence' to each tender in the list.
    Call this in send_tender_intelligence_digest before building the email HTML.

    Args:
        tenders: List of tender dicts (from GebizTender model rows)
        vendor_history: Optional vendor-specific context for scoring
        max_classify: Cap to avoid slow loops on large tender lists

    Returns:
        Same list with 'bid_label', 'bid_reason', 'bid_confidence' added to each item.
    """
    enriched = []
    for i, tender in enumerate(tenders):
        if i >= max_classify:
            # Beyond the cap, default to WATCH to avoid false signals
            enriched.append({
                **tender,
                "bid_label": "WATCH",
                "bid_reason": "Not classified (beyond analysis limit).",
                "bid_confidence": 0,
            })
            continue
        try:
            classification = classify_tender(tender, vendor_history)
        except Exception as e:
            logger.warning("[BidClassifier] Failed for tender %s: %s", tender.get("tender_no"), e)
            classification = {"label": "WATCH", "reason": "Classification error.", "confidence": 0}

        enriched.append({
            **tender,
            "bid_label": classification["label"],
            "bid_reason": classification["reason"],
            "bid_confidence": classification["confidence"],
        })
    return enriched


def build_vendor_history(db, vendor_id: str, sector: str, agency: str | None = None) -> dict:
    """
    Build the vendor_history dict from data that ACTUALLY EXISTS in the DB.

    IMPORTANT — read before changing this function:
    There is no table that records "vendor X won tender Y". GebizAwardHistory
    has a free-text supplier_name from GeBIZ with no foreign key to our
    vendor_id (GeBIZ doesn't know who our vendors are). TenderCheckLookup
    records that a vendor CHECKED a tender, not whether they won it.
    GeBizActivity records a generic status, not a win/loss outcome.

    This means true sector_win_rate / agency_win_rate per vendor CANNOT be
    computed from the database as it stands. Attempting a fuzzy match between
    User.company_name and GebizAwardHistory.supplier_name (e.g. "Acme Pte Ltd"
    vs "ACME PL") would silently produce wrong numbers — confident-looking
    but unverifiable. We do not do that here.

    What this function returns instead, honestly:
      - sector_win_rate: the vendor's SELF-REPORTED win_rate from LeadCapture
        if they provided one at signup, else the classifier's neutral default
        (15%). This is explicitly NOT a verified figure.
      - agency_win_rate: 0.0 (no real signal exists yet) — classify_tender's
        scoring treats this as "no prior relationship", which is the
        epistemically honest default.
      - avg_bid_size: derived from LeadCapture.avg_tender_value if the vendor
        self-reported it; else the SECTOR MEDIAN award value from real GeBIZ
        award history (a sector-level estimate, tagged avg_bid_size_source=
        "sector_median"), else None. This is deliberately a sector figure, not a
        fabricated per-vendor number, so the classifier's contract-size-fit
        factor can contribute instead of leaving every tender stuck at WATCH.
      - open_bids: count of DISTINCT tender_no the vendor checked in the last
        14 days via TenderCheckLookup. This is a real, measured proxy for
        "active attention", not a count of actual open bids — documented
        as such in the returned dict's _data_quality field.

    Until a real win/loss outcome table exists (e.g. a vendor self-reporting
    "I won this tender_no" with optional verification), sector_win_rate and
    agency_win_rate for any individual vendor are estimates, not facts. The
    classifier output (classify_tender) must not be presented to the customer
    as "we know your win rate" — present it as "based on sector averages and
    your declared history".
    """
    from datetime import datetime, timedelta, timezone

    history: dict = {
        "sector_win_rate": 0.15,   # neutral default — see docstring
        "agency_win_rate": 0.0,    # no real signal exists — see docstring
        "avg_bid_size": None,
        "open_bids": 0,
        "_data_quality": "estimated",  # surfaced to caller; never hide this
    }

    try:
        from app.core.models import LeadCapture
        from app.core.models import User

        user = db.query(User).filter(User.id == vendor_id).first()
        lead = None
        if user and user.email:
            lead = (
                db.query(LeadCapture)
                .filter(LeadCapture.email == user.email)
                .order_by(LeadCapture.created_at.desc())
                .first()
            )

        if lead and lead.win_rate is not None:
            # Self-reported at signup. Real number from the vendor, but
            # unverified by us — hence still "_data_quality": "estimated".
            history["sector_win_rate"] = max(0.0, min(1.0, lead.win_rate / 100))
        if lead and lead.avg_tender_value:
            history["avg_bid_size"] = float(lead.avg_tender_value)

    except Exception as e:
        logger.warning("[VendorHistory] LeadCapture lookup failed for %s: %s", vendor_id, e)

    # B.2 (honest sector signals): when the vendor hasn't self-reported an
    # avg_bid_size, fall back to the SECTOR MEDIAN award value from real GeBIZ
    # award history. This is a defensible SECTOR-level number (not a fabricated
    # per-vendor win rate), and it lets classify_tender's contract-size-fit
    # factor contribute instead of being skipped — which is what left every
    # tender stuck at WATCH. Explicitly NOT a vendor-specific figure.
    if history["avg_bid_size"] is None:
        try:
            median = _sector_median_award(db, sector)
            if median:
                history["avg_bid_size"] = median
                history["avg_bid_size_source"] = "sector_median"
        except Exception as e:
            logger.warning("[VendorHistory] sector median lookup failed for %s: %s", vendor_id, e)

    try:
        from app.core.models import TenderCheckLookup
        since = datetime.now(timezone.utc) - timedelta(days=14)
        open_bids = (
            db.query(TenderCheckLookup.tender_no)
            .filter(
                TenderCheckLookup.vendor_id == vendor_id,
                TenderCheckLookup.created_at >= since.replace(tzinfo=None),
            )
            .distinct()
            .count()
        )
        history["open_bids"] = open_bids
    except Exception as e:
        logger.warning("[VendorHistory] TenderCheckLookup lookup failed for %s: %s", vendor_id, e)

    return history


def _sector_median_award(db, sector: str | None) -> float | None:
    """Median award value for a sector from real GeBIZ award history.

    A defensible SECTOR-level signal (not per-vendor). Returns None when there
    isn't enough data (fewer than 3 awards) so the size-fit factor stays off
    rather than resting on a single noisy data point.
    """
    if not sector:
        return None
    try:
        from sqlalchemy import func
        from app.core.models import GebizAwardHistory

        amounts = [
            float(r[0])
            for r in db.query(GebizAwardHistory.award_amt)
            .filter(
                func.upper(GebizAwardHistory.sector) == sector.upper(),
                GebizAwardHistory.award_amt.isnot(None),
                GebizAwardHistory.award_amt > 0,
            )
            .all()
            if r[0] is not None
        ]
    except Exception as e:
        logger.warning("[VendorHistory] sector median query failed: %s", e)
        return None

    if len(amounts) < 3:
        return None
    amounts.sort()
    n = len(amounts)
    mid = n // 2
    return amounts[mid] if n % 2 else (amounts[mid - 1] + amounts[mid]) / 2


def bid_label_to_html_badge(label: str) -> str:
    """Return an inline HTML badge for use in email templates."""
    styles = {
        "BID": "background:#16a34a;color:#fff;",
        "WATCH": "background:#d97706;color:#fff;",
        "PASS": "background:#6b7280;color:#fff;",
    }
    style = styles.get(label, styles["WATCH"])
    return (
        f'<span style="{style}padding:2px 8px;border-radius:4px;'
        f'font-size:11px;font-weight:bold;letter-spacing:.05em;">{label}</span>'
    )
