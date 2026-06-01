"""add vendor_scan_ledger for Buyer ladder scan quotas

Revision ID: 2026_06_01_0001
Revises: 2026_05_24_0001
Create Date: 2026-06-01

Buyer-ladder marketing promises specific monthly scan caps per tier:
  buyer_starter:    10 Quick Scans/mo
  buyer_pro:        50 Quick + 20 Deep Scans/mo
  buyer_enterprise: 100 Quick + 100 Deep + 15 Evidence Scans/mo

Backend had no tracking. This table is the per-(buyer, vendor, month, scan_type)
ledger. The unique constraint gives insert-or-noop semantics: re-scanning the
same vendor within the same month for the same scan tier is free.

Limits live in app/billing/enforcement.py::BUYER_SCAN_LIMITS.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_01_0001"
down_revision = "2026_05_24_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_scan_ledger",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "buyer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("scan_type", sa.String(length=20), nullable=False),
        sa.Column("plan_at_consumption", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "buyer_id", "vendor_id", "month", "scan_type",
            name="uq_scan_ledger_buyer_vendor_month_type",
        ),
    )
    op.create_index(
        "ix_scan_ledger_buyer_month_type",
        "vendor_scan_ledger",
        ["buyer_id", "month", "scan_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_scan_ledger_buyer_month_type", table_name="vendor_scan_ledger")
    op.drop_table("vendor_scan_ledger")
