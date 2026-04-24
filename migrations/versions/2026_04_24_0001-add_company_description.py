"""add company_description to users

Revision ID: 2026_04_24_0001
Revises: 2026_04_21_0001
Create Date: 2026-04-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_04_24_0001"
down_revision = "2026_04_21_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("company_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "company_description")
