"""add compliance_drift_events table

Revision ID: 2026_05_10_0001
Revises: 2026_05_02_0001
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_05_10_0001"
down_revision = "2026_05_02_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compliance_drift_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("framework", sa.String(64), nullable=False, server_default="pdpa_quick_scan"),
        sa.Column("previous_report_id", UUID(as_uuid=True), nullable=True),
        sa.Column("current_report_id", UUID(as_uuid=True), nullable=True),
        sa.Column("previous_score", sa.Float(), nullable=True),
        sa.Column("current_score", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("delta_pct", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="WARNING"),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_compliance_drift_vendor_created",
        "compliance_drift_events",
        ["vendor_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_compliance_drift_vendor_created", table_name="compliance_drift_events")
    op.drop_table("compliance_drift_events")
