"""add vendor_tender_alerts_sent table

Revision ID: 2026_06_26_0002
Revises: 2026_06_26_0001
Create Date: 2026-06-26

Dedup ledger for the daily BID-tender alert email — one row per (vendor, tender)
already alerted, so an open tender is emailed at most once per vendor.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_26_0002"
down_revision = "2026_06_26_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_tender_alerts_sent",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("vendor_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tender_no", sa.String(length=100), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("vendor_id", "tender_no", name="uq_vendor_tender_alert_sent"),
    )
    op.create_index("ix_vendor_tender_alerts_sent_vendor_id", "vendor_tender_alerts_sent", ["vendor_id"])
    op.create_index("ix_vendor_tender_alerts_sent_tender_no", "vendor_tender_alerts_sent", ["tender_no"])


def downgrade() -> None:
    op.drop_index("ix_vendor_tender_alerts_sent_tender_no", table_name="vendor_tender_alerts_sent")
    op.drop_index("ix_vendor_tender_alerts_sent_vendor_id", table_name="vendor_tender_alerts_sent")
    op.drop_table("vendor_tender_alerts_sent")
