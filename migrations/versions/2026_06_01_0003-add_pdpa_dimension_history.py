"""add pdpa_dimension_history for per-dimension drift detection

Revision ID: 2026_06_01_0003
Revises: 2026_06_01_0002
Create Date: 2026-06-04

ComplianceDriftEvent today only tracks overall risk_score deltas. After
PDPA scanner Tiers 1-3, each report carries per-dimension status/score
(NRIC Exposure, Cross-Border Transfer, Privacy Policy §13, etc.).
This table stores one row per dimension per completed scan so the
monthly drift task can detect dimension-level Compliant → Non-Compliant
flips that overall scoring would otherwise hide.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_01_0003"
down_revision = "2026_06_01_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdpa_dimension_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("report_id", UUID(as_uuid=True), nullable=True),
        sa.Column("framework", sa.String(64), nullable=False, server_default="pdpa_quick_scan"),
        sa.Column("dimension_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_pdpa_dim_history_vendor_dim_time",
        "pdpa_dimension_history",
        ["vendor_id", "dimension_name", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pdpa_dim_history_vendor_dim_time", table_name="pdpa_dimension_history")
    op.drop_table("pdpa_dimension_history")
