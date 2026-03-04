"""
Commercial Activation Layer (CAL) — V8
=======================================
Pure functions for the vendor dashboard activation ladder,
upgrade suggestion, and dynamic message rendering.

NO DB CALLS in this module. All inputs come from caller.
Designed to be pure and unit-testable.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Ladder levels ─────────────────────────────────────────────────────────────
LADDER_LEVELS = [
    {
        "level":       "STARTER",
        "label":       "Starter — Account created",
        "description": "Your vendor account is active.",
        "check":       lambda v, _: True,   # always met
    },
    {
        "level":       "VERIFIED",
        "label":       "Verified — Compliance document submitted",
        "description": "Submit at least one compliance document.",
        "check":       lambda v, _: v.get("compliance_score", 0) > 0,
    },
    {
        "level":       "NOTARIZED",
        "label":       "Notarized — First notarization complete",
        "description": "Complete your first notarization to reach ELEVATED status.",
        "check":       lambda v, _: v.get("is_elevated", False),
    },
    {
        "level":       "PROMINENT",
        "label":       "Prominent — Top 25% in sector",
        "description": "Reach the top 25 percentile in your sector.",
        "check":       lambda v, s: (s.get("vendorRank") or 999) <= max(1, round(s.get("totalInSector", 1) * 0.25)),
    },
    {
        "level":       "ELITE",
        "label":       "Elite — ENTERPRISE depth achieved",
        "description": "Complete 6+ notarizations or 3+ evidence packages.",
        "check":       lambda v, _: v.get("evidence_count", 0) >= 6,
    },
]


def analyze_activation_gaps(vendor: dict, sector_pressure: dict) -> dict:
    """
    Determine which ladder levels are met and what comes next.

    vendor must contain:
      compliance_score, is_elevated, evidence_count, confidence_score,
      plan, tier.
    sector_pressure is from get_sector_competitive_pressure().

    Returns:
      {
        levels: [{level, label, description, met: bool}],
        highestMet: str | None,
        nextLevel: str | None,
        gapScore: int,           # 0–100 distance to next level
        progressPct: int,        # 0–100 overall ladder completion
      }
    """
    levels = []
    highest_idx = -1

    for i, lvl in enumerate(LADDER_LEVELS):
        met = lvl["check"](vendor, sector_pressure)
        if met:
            highest_idx = i
        levels.append({
            "level":       lvl["level"],
            "label":       lvl["label"],
            "description": lvl["description"],
            "met":         met,
        })

    highest_met = levels[highest_idx]["level"] if highest_idx >= 0 else None
    next_level  = levels[highest_idx + 1]["level"] if highest_idx < len(LADDER_LEVELS) - 1 else None

    progress_pct = round((highest_idx + 1) / len(LADDER_LEVELS) * 100)

    # Gap score: how close to next level (heuristic based on evidence & rank)
    gap_score = 0
    if next_level == "VERIFIED":
        gap_score = 100 if vendor.get("compliance_score", 0) == 0 else 20
    elif next_level == "NOTARIZED":
        gap_score = 60    # need notarization
    elif next_level == "PROMINENT":
        rank = sector_pressure.get("vendorRank") or 999
        total = max(sector_pressure.get("totalInSector", 1), 1)
        gap_score = min(100, round((rank / total) * 100))
    elif next_level == "ELITE":
        count = vendor.get("evidence_count", 0)
        gap_score = max(0, 100 - round((count / 6) * 100))
    else:
        gap_score = 0   # at top

    return {
        "levels":       levels,
        "highestMet":   highest_met,
        "nextLevel":    next_level,
        "gapScore":     gap_score,
        "progressPct":  progress_pct,
    }


def generate_upgrade_suggestion(vendor: dict, gap_analysis: dict, total_elevated: int) -> dict:
    """
    Returns an upgrade suggestion with a probability score (0–100) and insight text.
    Informational only — never triggers a conversion action.
    """
    next_level       = gap_analysis.get("nextLevel")
    gap_score        = gap_analysis.get("gapScore", 0)
    progress_pct     = gap_analysis.get("progressPct", 0)
    is_elevated      = vendor.get("is_elevated", False)
    confidence_score = vendor.get("confidence_score", 0)
    evidence_count   = vendor.get("evidence_count", 0)

    # Probability: higher gap + nearby elevated peers = higher upgrade likelihood
    peer_pressure = min(total_elevated * 5, 30)
    evidence_bonus = min(evidence_count * 3, 20)
    confidence_bonus = round(confidence_score * 0.2)

    probability = min(100, round(
        (gap_score * 0.4)
        + peer_pressure
        + evidence_bonus
        + confidence_bonus
        + (20 if is_elevated else 0)
    ))

    insight = _upgrade_insight(next_level, is_elevated, total_elevated, gap_score)

    return {
        "nextLevel":         next_level,
        "probabilityScore":  probability,
        "insight":           insight,
        "progressPct":       progress_pct,
    }


def render_message(ctx: dict) -> str:
    """
    Renders a dynamic CAL message based on the gap × sector density matrix.

    ctx keys:
      vendor, sector, peerAvgEvidence, vendorEvidence,
      top3AvgEvidence, recentlyActiveCount, totalElevatedPeers,
      gapAnalysis, suggestion.
    """
    vendor     = ctx.get("vendor", {})
    gap        = ctx.get("gapAnalysis", {})
    suggestion = ctx.get("suggestion", {})
    next_level = gap.get("nextLevel")
    progress   = gap.get("progressPct", 0)
    sector     = ctx.get("sector", "your sector")
    total      = ctx.get("totalElevatedPeers", 0)
    active     = ctx.get("recentlyActiveCount", 0)

    if not next_level:
        return (
            f"Congratulations! You have reached the highest activation level. "
            f"There are {total} ELEVATED vendors in {sector}. Maintain your evidence to stay ahead."
        )

    if next_level == "VERIFIED":
        return (
            f"Start by submitting a compliance document. "
            f"Verified vendors appear in more procurement search results."
        )

    if next_level == "NOTARIZED":
        return (
            f"Complete your first notarization to unlock ELEVATED status. "
            f"{total} vendors in {sector} have already done so. "
            f"ELEVATED vendors are prioritised in procurement searches at no cost change."
        )

    if next_level == "PROMINENT":
        prob = suggestion.get("probabilityScore", 0)
        return (
            f"You are {progress}% through the activation ladder. "
            f"Add more evidence to reach the top 25% in {sector}. "
            f"({active} vendors were active in this sector in the last 30 days)"
        )

    if next_level == "ELITE":
        return (
            f"You are {progress}% through. "
            f"Completing 6 notarizations delivers ENTERPRISE-level trust signals "
            f"to procurement teams reviewing your profile."
        )

    return f"Keep building your verification profile. You are {progress}% through the activation ladder."


# ── Private ───────────────────────────────────────────────────────────────────

def _upgrade_insight(next_level: Optional[str], is_elevated: bool, total_elevated: int, gap_score: int) -> str:
    if not next_level:
        return "You have reached the highest activation level."
    if next_level == "VERIFIED":
        return "Add a compliance document to start building your procurement profile."
    if next_level == "NOTARIZED":
        return (
            f"Notarization unlocks ELEVATED procurement visibility. "
            f"{total_elevated} competitors have already done so."
        )
    if next_level == "PROMINENT":
        return "Increase your evidence count to rank higher in sector-specific procurement searches."
    if next_level == "ELITE":
        return "Enterprise-grade trust signals require 6+ notarizations. Each one strengthens your profile."
    return "Continue building your profile to unlock the next activation level."
