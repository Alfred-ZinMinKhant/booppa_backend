"""add search_impressions table

Revision ID: 2026_06_25_0001
Revises: 2026_06_23_0001
Create Date: 2026-06-25

Logs one row per appearance of a claimed vendor in a buyer search result so the
Vendor Active monthly snapshot can report "your profile appeared in N searches
this month" instead of an honest-but-empty placeholder. Written best-effort by
the marketplace / discovery search endpoints.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_25_0001"
down_revision = "2026_06_23_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_impressions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("vendor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("query", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_search_impressions_vendor_id", "search_impressions", ["vendor_id"]
    )
    op.create_index(
        "ix_search_impressions_created_at", "search_impressions", ["created_at"]
    )
    op.create_index(
        "ix_search_impressions_vendor_created",
        "search_impressions",
        ["vendor_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_search_impressions_vendor_created", table_name="search_impressions")
    op.drop_index("ix_search_impressions_created_at", table_name="search_impressions")
    op.drop_index("ix_search_impressions_vendor_id", table_name="search_impressions")
    op.drop_table("search_impressions")
