"""add deep_scan_dimension_history for Deep Scan (Phase 3)

Revision ID: 2026_07_18_0001
Revises: 2026_07_14_0001
Create Date: 2026-07-18

The Deep Scan (`deep_scan_service` + `run_deep_scan_task`) writes one row per
dimension per scan: 11 PDPA obligations + a Certifications dimension + a
Financial Risk dimension, all derived from freshly-fetched registry / website /
security signals. Feeds `/procurement/snapshot/{slug}` and Phase-4 Deep-Scan
drift detection (reuses `diff_snapshots`).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "2026_07_18_0001"
down_revision = "2026_07_14_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deep_scan_dimension_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("scan_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("category", sa.String(32), nullable=False, server_default="pdpa"),
        sa.Column("dimension_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_deep_scan_dim_vendor_dim_time",
        "deep_scan_dimension_history",
        ["vendor_id", "dimension_name", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deep_scan_dim_vendor_dim_time", table_name="deep_scan_dimension_history")
    op.drop_table("deep_scan_dimension_history")
