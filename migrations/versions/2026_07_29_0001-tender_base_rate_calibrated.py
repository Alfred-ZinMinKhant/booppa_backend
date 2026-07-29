"""mark whether a tender's base_rate was calibrated from real history

Incident: every tender in a vendor's digest showed the same win probability
(30.2%) across unrelated sectors. The multiplier chain in
`tender_service._compute_raw_probability` is fine — it just had nothing to
differentiate. Tenders auto-created from the GeBIZ RSS sync
(`tender_service.py`, `workers/tasks.py`) all get a hardcoded
`base_rate = 0.20` placeholder, so for a vendor whose profile multipliers are
identical across those tenders the product is identical too.

That number is not wrong, but it is presented as if it were tender-specific.
This column records which rows carry a real sector/agency-calibrated base rate
and which carry the placeholder, so the renderers can caveat the latter
("~30% — baseline estimate, limited history for this category") instead of
quoting a false precision.

Backfill is a heuristic: `base_rate = 0.20` is exactly the placeholder the two
auto-create paths write. A hand-imported tender that genuinely calibrated to
0.20 is mislabelled as uncalibrated — that direction is safe (it only adds a
caveat), the reverse would not be. Forward rows set the flag explicitly.

Revision ID: 2026_07_29_0001
Revises: 2026_07_28_0003
"""
import sqlalchemy as sa
from alembic import op

revision = "2026_07_29_0001"
down_revision = "2026_07_28_0003"
branch_labels = None
depends_on = None

PLACEHOLDER_BASE_RATE = 0.20


def upgrade() -> None:
    # server_default=true so the column is NOT NULL from the first row and
    # existing/admin-imported tenders keep their current meaning by default.
    op.add_column(
        "tender_shortlists",
        sa.Column(
            "base_rate_calibrated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        "UPDATE tender_shortlists SET base_rate_calibrated = false "
        f"WHERE base_rate = {PLACEHOLDER_BASE_RATE}"
    )


def downgrade() -> None:
    op.drop_column("tender_shortlists", "base_rate_calibrated")
