"""add website to users

Revision ID: 2026_04_14_0002
Revises: 2026_04_14_0001
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_04_14_0002"
down_revision = "2026_04_14_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("website", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "website")
