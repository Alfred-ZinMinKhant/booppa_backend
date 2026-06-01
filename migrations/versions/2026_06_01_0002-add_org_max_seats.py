"""add organisations.max_seats for team-collaboration seat enforcement

Revision ID: 2026_06_01_0002
Revises: 2026_06_01_0001
Create Date: 2026-06-01

Marketing promises seat limits per buyer-ladder tier:
  buyer_starter: 1 seat
  buyer_pro:     3 seats
  buyer_enterprise: unlimited

Backend had no `max_seats` column on Organisation. This adds it as nullable
INTEGER (NULL = unlimited). New orgs get the value from PLAN_TO_MAX_SEATS at
activation; existing rows backfill to NULL (treated as unlimited — we don't
retroactively shrink a customer's seat cap).
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_06_01_0002"
down_revision = "2026_06_01_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("max_seats", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organisations", "max_seats")
