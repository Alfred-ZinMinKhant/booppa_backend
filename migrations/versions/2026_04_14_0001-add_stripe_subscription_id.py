"""add stripe_subscription_id to users

Revision ID: 2026_04_14_0001
Revises: 2026_04_12_0001
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_04_14_0001"
down_revision = "2026_04_12_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stripe_subscription_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "stripe_subscription_id")
