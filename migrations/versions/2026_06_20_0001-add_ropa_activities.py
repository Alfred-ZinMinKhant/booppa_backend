"""add ropa_activities table

Revision ID: 2026_06_20_0001
Revises: 2026_06_16_0002
Create Date: 2026-06-20

ROPA Lite (Record of Processing Activities) — PDPC Level 2 evidence for the
Compliance Evidence Pack. One row per declared processing activity, draft →
submitted lifecycle, user_id-based identification (mirrors pending_rfp_intakes).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_06_20_0001"
down_revision = "2026_06_16_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ropa_activities",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bundle_source",
            sa.String(length=64),
            nullable=False,
            server_default="compliance_evidence_pack",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("processing_purpose", sa.String(length=200), nullable=False),
        sa.Column("data_categories", sa.String(length=500), nullable=False),
        sa.Column("data_subjects", sa.String(length=200), nullable=False),
        sa.Column("retention_period", sa.String(length=300), nullable=False),
        sa.Column("cross_border_transfer", sa.String(length=400), nullable=False),
        sa.Column("legal_basis", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_ropa_activities_user_id",
        "ropa_activities",
        ["user_id"],
    )
    op.create_index(
        "ix_ropa_activities_user_status",
        "ropa_activities",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_ropa_activities_user_status", table_name="ropa_activities")
    op.drop_index("ix_ropa_activities_user_id", table_name="ropa_activities")
    op.drop_table("ropa_activities")
