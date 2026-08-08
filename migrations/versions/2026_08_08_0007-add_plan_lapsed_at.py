"""add users.plan_lapsed_at / users.lapsed_plan

Backs the 90-day post-cancellation read-only grace promised at
booppa-nextjs/app/pricing/page.tsx:24. Before this, `User` carried only `plan`,
which the cancel branch rewrites to "free" — so nothing recorded either WHEN a
subscription lapsed or WHICH tier it was, and the promise was unenforceable in
both directions.

Both columns are nullable with no backfill: existing lapsed users have no
recorded cancellation date, so they get no grace window rather than a fabricated
one starting at migration time. Granting 90 days to every historically-free
account would be worse than the bug.

Revision ID: 2026_08_08_0007
Revises: 2026_08_08_0006
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_08_08_0007"
down_revision = "2026_08_08_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("plan_lapsed_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("lapsed_plan", sa.String(50), nullable=True))


def downgrade():
    op.drop_column("users", "lapsed_plan")
    op.drop_column("users", "plan_lapsed_at")
