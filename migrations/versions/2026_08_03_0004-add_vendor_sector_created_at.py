"""add created_at to vendor_sectors

`vendor_sectors` was append-only with no ordering column, and every reader did
an unordered `.first()`. A vendor that accumulated rows therefore had no stable
primary sector — Postgres decided, per query.

Deliberately nullable with no backfill. Existing rows have no knowable creation
order; stamping them all with `now()` would manufacture a total order that is
pure fiction and would let `resolve_primary_sector` pick a "newest" row on the
strength of it. Leaving them NULL is what lets the resolver report the vendor as
unresolved instead, which is the honest answer for those rows.

Revision ID: 2026_08_03_0004
Revises: 2026_08_03_0003
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_08_03_0004"
down_revision = "2026_08_03_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_sectors",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vendor_sectors", "created_at")
