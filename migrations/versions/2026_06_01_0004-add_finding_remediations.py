"""add finding_remediations for user-marked compliance fixes

Revision ID: 2026_06_01_0004
Revises: 2026_06_01_0003
Create Date: 2026-06-04

Customers can mark individual PDPA findings as "fixed" (or won't-fix) from
their report view. On the next scan, the worker auto-confirms whether the
finding actually disappeared (confirmed) or still appears (regressed).
This lets users earn credit for remediation work and gives drift detection
a concept of "improvement" alongside the existing "regression" path.

`finding_key` is a stable string derived from the finding type — see
app/services/finding_keys.py — so the same finding across scans hashes to
the same key and confirmation logic can match.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_01_0004"
down_revision = "2026_06_01_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finding_remediations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("report_id", UUID(as_uuid=True), nullable=True),
        sa.Column("finding_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="fixed"),
        # ^ open | fixed | wontfix
        sa.Column(
            "confirmation_status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        # ^ pending | confirmed | regressed | stale
        sa.Column("marked_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("marked_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime, nullable=True),
        sa.Column("confirming_report_id", UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_finding_remediations_vendor_key_time",
        "finding_remediations",
        ["vendor_id", "finding_key", "marked_at"],
    )
    op.create_index(
        "ix_finding_remediations_vendor_status",
        "finding_remediations",
        ["vendor_id", "confirmation_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_finding_remediations_vendor_status", table_name="finding_remediations")
    op.drop_index("ix_finding_remediations_vendor_key_time", table_name="finding_remediations")
    op.drop_table("finding_remediations")
