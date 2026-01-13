"""Add company_website column to reports table

Revision ID: 0001_add_company_website
Revises:
Create Date: 2026-01-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_add_company_website"
down_revision = None
branch_labels = None
dependencies = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("company_website", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reports", "company_website")
