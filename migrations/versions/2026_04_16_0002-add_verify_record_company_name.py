"""add company_name to verify_records

Revision ID: 2026_04_16_0002
Revises: 2026_04_16_0001
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_04_16_0002"
down_revision = "2026_04_16_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verify_records", sa.Column("company_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("verify_records", "company_name")
