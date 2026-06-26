"""
Scan engine versioning
======================
Stable version stamps written into every PDPA scan's report payload so a reader
(support, the customer, an auditor) can tell *which* version of the scan schema
and scoring formula produced a given report. This makes score changes between
two scans attributable to "the engine changed" vs "the website changed" — the
ambiguity behind the historical 53-vs-54 score drift.

Bump these when:
  • PDPA_SCHEMA_VERSION  — the shape/keys of assessment_data change
  • PDPA_SCORING_VERSION — the dimension weights or score formula change
"""
from __future__ import annotations

# assessment_data structure (dimensions, findings shape, metadata keys).
PDPA_SCHEMA_VERSION = "1.0"

# Dimension weighting + risk→compliance formula in pdpa_dimension_snapshot /
# booppa_ai_service. Bump on any change that would move a score for an
# unchanged website.
PDPA_SCORING_VERSION = "1.0"


def scan_version_meta() -> dict[str, str]:
    """Version stamp to merge into report_metadata / assessment_data."""
    return {
        "schema_version": PDPA_SCHEMA_VERSION,
        "scoring_version": PDPA_SCORING_VERSION,
    }
