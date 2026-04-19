"""add industry to users

Revision ID: 2026_04_19_0001
Revises: 2026_04_18_0001
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_04_19_0001"
down_revision = "2026_04_18_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("industry", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "industry")
