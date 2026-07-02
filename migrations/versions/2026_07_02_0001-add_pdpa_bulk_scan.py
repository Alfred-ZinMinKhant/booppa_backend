"""PDPA bulk scan — admin CSV/XLSX batch of free scans

Revision ID: 2026_07_02_0001
Revises: 2026_06_27_0002
Create Date: 2026-07-02

Admin-only bulk testing tool: one batch row per uploaded file, one item row per
company. Items are processed by the rate-limited bulk_pdpa_scan_item_task on the
`reports` queue; results land in the item's JSONB `result`.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "2026_07_02_0001"
down_revision = "2026_06_27_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdpa_bulk_scan_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "pdpa_bulk_scan_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pdpa_bulk_scan_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("website_url", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pdpa_bulk_scan_items_batch_id", "pdpa_bulk_scan_items", ["batch_id"])
    op.create_index(
        "ix_pdpa_bulk_items_batch_status", "pdpa_bulk_scan_items", ["batch_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("pdpa_bulk_scan_items")
    op.drop_table("pdpa_bulk_scan_batches")
