"""add sector column to organisations

Revision ID: 2026_06_23_0001
Revises: 2026_06_20_0003
Create Date: 2026-06-23

Drives sector-priority ordering of the 13 MAS TRM domains (fintech/healthcare
lead with their material domains). Nullable VARCHAR — existing orgs and orgs
with no sector fall back to canonical TRM-1..TRM-13 order.
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_06_23_0001"
down_revision = "2026_06_20_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("sector", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organisations", "sector")
