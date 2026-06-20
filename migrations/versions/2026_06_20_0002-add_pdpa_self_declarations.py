"""add pdpa_self_declarations table

Revision ID: 2026_06_20_0002
Revises: 2026_06_20_0001
Create Date: 2026-06-20

PDPA Level-2 self-declaration intake — one row per declared processing activity,
draft → submitted lifecycle, user_id-based identification (mirrors ropa_activities).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_06_20_0002"
down_revision = "2026_06_20_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdpa_self_declarations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="pdpa_quick_scan"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("processing_purpose", sa.String(length=200), nullable=False),
        sa.Column("lawful_basis", sa.String(length=100), nullable=False),
        sa.Column("data_categories", sa.String(length=500), nullable=False),
        sa.Column("data_subjects", sa.String(length=200), nullable=False),
        sa.Column("recipients", sa.String(length=400), nullable=False),
        sa.Column("retention_period", sa.String(length=300), nullable=False),
        sa.Column("safeguards", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_pdpa_self_declarations_user_id",
        "pdpa_self_declarations",
        ["user_id"],
    )
    op.create_index(
        "ix_pdpa_self_declarations_user_status",
        "pdpa_self_declarations",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_pdpa_self_declarations_user_status", table_name="pdpa_self_declarations")
    op.drop_index("ix_pdpa_self_declarations_user_id", table_name="pdpa_self_declarations")
    op.drop_table("pdpa_self_declarations")
