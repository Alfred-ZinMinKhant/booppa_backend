"""
my_reality_check_service.py — My Reality Check Service (GTM v9 §2.3).

Provides personalized compliance score benchmarks, sector rank, and 90-day plan.
"""

from dataclasses import dataclass
from statistics import mean
from typing import Dict, List

SCORE_DIMENSIONS = [
    "compliance_score", "visibility_score", "engagement_score",
    "recency_score", "procurement_interest_score",
]

DIMENSION_LABELS = {
    "compliance_score": "PDPA / regulatory compliance",
    "visibility_score": "Visibility in catalogue / buyer searches",
    "engagement_score": "Profile engagement (updates, responses)",
    "recency_score": "Recency of data updates",
    "procurement_interest_score": "Buyer interest (views, requests)",
}

NINETY_DAY_ACTIONS = {
    "compliance_score": "Complete the PDPA Quick Scan and fix the gaps found within 30 days; consider Trust Passport L2 (Notarised) to make the evidence independently verifiable.",
    "visibility_score": "Complete every field on the public profile and add at least 2 verifiable certifications within 45 days.",
    "engagement_score": "Respond to every verification request within 48 hours and update the profile at least once a month.",
    "recency_score": "Activate Trust Passport + Monitor for an automatic refresh instead of sporadic manual updates.",
    "procurement_interest_score": "Register for the relevant GeBIZ categories and complete the tender-history section of the profile, if available.",
}


@dataclass
class SectorBenchmark:
    sector: str
    dimension_averages: Dict[str, float]
    total_score_average: float
    vendor_count: int


@dataclass
class RealityCheckResult:
    vendor_id: str
    vendor_total_score: int
    sector: str
    sector_average: float
    percentile_rank: float
    dimension_scores: Dict[str, int]
    dimension_vs_sector: Dict[str, float]
    top_gaps: List[dict]
    ninety_day_plan: List[str]


def compute_sector_benchmark(sector: str, sector_vendor_scores: List[dict]) -> SectorBenchmark:
    if not sector_vendor_scores:
        raise ValueError("sector_vendor_scores cannot be empty")

    dim_avgs = {
        dim: mean(v.get(dim, 0) for v in sector_vendor_scores)
        for dim in SCORE_DIMENSIONS
    }
    total_avg = mean(v.get("total_score", 0) for v in sector_vendor_scores)

    return SectorBenchmark(
        sector=sector,
        dimension_averages=dim_avgs,
        total_score_average=total_avg,
        vendor_count=len(sector_vendor_scores),
    )


def compute_percentile_rank(vendor_total_score: int, sector_totals: List[int]) -> float:
    if not sector_totals:
        return 100.0
    n_at_or_below = sum(1 for s in sector_totals if s <= vendor_total_score)
    return round(100 * n_at_or_below / len(sector_totals), 1)


def identify_top_gaps(
    dimension_scores: Dict[str, int], sector_benchmark: SectorBenchmark, top_n: int = 3
) -> List[dict]:
    gaps = []
    for dim in SCORE_DIMENSIONS:
        vendor_val = dimension_scores.get(dim, 0)
        sector_avg = sector_benchmark.dimension_averages.get(dim, 0)
        gap = vendor_val - sector_avg
        gaps.append({
            "dimension": dim,
            "label": DIMENSION_LABELS[dim],
            "vendor_score": vendor_val,
            "sector_average": round(sector_avg, 1),
            "gap": round(gap, 1),
        })

    gaps.sort(key=lambda g: g["gap"])
    top = gaps[:top_n]
    for g in top:
        g["recommended_action"] = NINETY_DAY_ACTIONS[g["dimension"]]
    return top


def build_reality_check(
    vendor_id: str, sector: str, vendor_scores: dict, sector_vendor_scores: List[dict]
) -> RealityCheckResult:
    benchmark = compute_sector_benchmark(sector, sector_vendor_scores)
    sector_totals = [v.get("total_score", 0) for v in sector_vendor_scores]
    percentile = compute_percentile_rank(vendor_scores.get("total_score", 0), sector_totals)

    dimension_vs_sector = {
        dim: round(vendor_scores.get(dim, 0) - benchmark.dimension_averages.get(dim, 0), 1)
        for dim in SCORE_DIMENSIONS
    }

    top_gaps = identify_top_gaps(vendor_scores, benchmark)
    plan = [g["recommended_action"] for g in top_gaps]

    return RealityCheckResult(
        vendor_id=vendor_id,
        vendor_total_score=vendor_scores.get("total_score", 0),
        sector=sector,
        sector_average=round(benchmark.total_score_average, 1),
        percentile_rank=percentile,
        dimension_scores={dim: vendor_scores.get(dim, 0) for dim in SCORE_DIMENSIONS},
        dimension_vs_sector=dimension_vs_sector,
        top_gaps=top_gaps,
        ninety_day_plan=plan,
    )
