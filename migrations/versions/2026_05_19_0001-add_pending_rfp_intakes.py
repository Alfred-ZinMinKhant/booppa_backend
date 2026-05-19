"""add pending_rfp_intakes table

Revision ID: 2026_05_19_0001
Revises: 2026_05_17_0001
Create Date: 2026-05-19

Bundle SKUs containing an RFP component now defer RFP generation until the
buyer submits a brief. This table tracks one row per pending submission.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_05_19_0001"
down_revision = "2026_05_17_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_rfp_intakes",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("rfp_product_type", sa.String(length=64), nullable=False),
        sa.Column("bundle_source", sa.String(length=64), nullable=False),
        sa.Column("vendor_url", sa.String(length=500), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_pending_rfp_intakes_user_id",
        "pending_rfp_intakes",
        ["user_id"],
    )
    op.create_index(
        "ix_pending_rfp_intakes_session_id",
        "pending_rfp_intakes",
        ["session_id"],
    )
    op.create_index(
        "ix_pending_rfp_user_status",
        "pending_rfp_intakes",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_rfp_user_status", table_name="pending_rfp_intakes")
    op.drop_index("ix_pending_rfp_intakes_session_id", table_name="pending_rfp_intakes")
    op.drop_index("ix_pending_rfp_intakes_user_id", table_name="pending_rfp_intakes")
    op.drop_table("pending_rfp_intakes")
