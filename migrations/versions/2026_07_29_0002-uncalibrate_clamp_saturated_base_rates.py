"""clamp-saturated base rates are not calibrations

Follow-up to 2026_07_29_0001. That migration made the flag honest about the
0.20 placeholder, but a second, larger population was quietly claiming
calibration it did not have: **515 of 523 production tenders sat at exactly
base_rate 0.60**, which is `CLAMP_MAX` in `refresh_gebiz_base_rates`.

The refresh computes

    base_rate = unique vendors awarded in sector / total tenders in sector
                clamped to [0.05, 0.60]

That ratio answers "was this tender awarded to anyone", not "does this bidder
win", so it runs near 1.0 and pins at the ceiling for almost every sector and
agency. `resolve_tender_base_rate` then marked the result calibrated purely
because a rate had been found, so the customer-facing baseline caveat was
suppressed on figures the clamp had decided. The visible symptom: a Vendor Pro
report listing ten unrelated tenders at an identical bare "56%", no "~", no
footnote — the exact false-precision failure 0001 was written to prevent.

`resolve_tender_base_rate` now reports `calibrated=False` at saturation, but it
only rewrites rows when the weekly refresh next runs. This backfills so the
caveat is correct immediately rather than up to a week late.

Heuristic, same shape as 0001: a tender that genuinely calibrated to exactly
0.60 is mislabelled as uncalibrated. That direction only adds a caveat, which is
the safe way to be wrong; the reverse quotes an uncalibrated number bare.

Revision ID: 2026_07_29_0002
Revises: 2026_07_29_0001
"""
from alembic import op

revision = "2026_07_29_0002"
down_revision = "2026_07_29_0001"
branch_labels = None
depends_on = None

CLAMP_MAX = 0.60


def upgrade() -> None:
    op.execute(
        "UPDATE tender_shortlists SET base_rate_calibrated = false "
        f"WHERE base_rate >= {CLAMP_MAX}"
    )


def downgrade() -> None:
    # Not reversible in a meaningful sense: the pre-migration value asserted a
    # calibration that never existed, so restoring it would restore the false
    # claim. The weekly refresh is the authority on this flag either way.
    pass
