"""add subscription_anniversary_day column to users

Persists the day-of-month (1-28) each subscriber's monthly cycle should fire,
so we can replace calendar-1st cron schedules with per-subscriber anniversary
delivery. Capped at 28 to side-step Feb / 31st edge cases.

Backfill: existing subscribers with `subscription_started_at` get their
anniversary set from that timestamp's `.day` (capped at 28). Users without
a start date get NULL — the cron tasks skip NULL silently so they're a no-op
until the next activation event sets the column.
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_06_07_0001"
down_revision = "2026_06_01_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("subscription_anniversary_day", sa.Integer(), nullable=True),
    )
    # Backfill from subscription_started_at for active subscribers. Cap at 28
    # so February cycles always have a matching day each month.
    op.execute(
        """
        UPDATE users
        SET subscription_anniversary_day = LEAST(EXTRACT(DAY FROM subscription_started_at)::int, 28)
        WHERE subscription_started_at IS NOT NULL
          AND subscription_tier IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("users", "subscription_anniversary_day")
