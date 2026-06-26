"""add vendor_tender_intents table

Revision ID: 2026_06_26_0001
Revises: 2026_06_25_0001
Create Date: 2026-06-26

Per-vendor BID/WATCH/PASS/NOT-BIDDING intent for live GeBIZ tenders. Powers the
in-app Tender Intelligence feed's action loop. Tender fields are snapshotted so a
tracked tender still renders after it leaves the live feed.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_06_26_0001"
down_revision = "2026_06_25_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_tender_intents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("vendor_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tender_no", sa.String(length=100), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("agency", sa.String(length=255), nullable=True),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("estimated_value", sa.Float(), nullable=True),
        sa.Column("closing_date", sa.DateTime(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(length=20), nullable=False, server_default="watch"),
        sa.Column("bid_label", sa.String(length=10), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("vendor_id", "tender_no", name="uq_vendor_tender_intent"),
    )
    op.create_index("ix_vendor_tender_intents_vendor_id", "vendor_tender_intents", ["vendor_id"])
    op.create_index("ix_vendor_tender_intents_tender_no", "vendor_tender_intents", ["tender_no"])
    op.create_index("ix_vendor_tender_intents_vendor", "vendor_tender_intents", ["vendor_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_vendor_tender_intents_vendor", table_name="vendor_tender_intents")
    op.drop_index("ix_vendor_tender_intents_tender_no", table_name="vendor_tender_intents")
    op.drop_index("ix_vendor_tender_intents_vendor_id", table_name="vendor_tender_intents")
    op.drop_table("vendor_tender_intents")
