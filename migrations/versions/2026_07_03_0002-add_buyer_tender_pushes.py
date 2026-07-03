"""Buyer per-tender high-fit push dedup ledger

Revision ID: 2026_07_03_0002
Revises: 2026_07_03_0001
Create Date: 2026-07-03

One row per (buyer, tender). Records that we already emailed this buyer an
immediate "strongly-matching tender just opened" push for a given GeBIZ tender,
so the ingest-triggered sweep never re-pushes the same tender.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_07_03_0002"
down_revision = "2026_07_03_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buyer_tender_pushes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "buyer_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tender_no", sa.String(100), nullable=False),
        sa.Column("sector", sa.String(255), nullable=True),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_buyer_tender_pushes_buyer_user_id", "buyer_tender_pushes", ["buyer_user_id"]
    )
    op.create_index(
        "ix_buyer_tender_pushes_tender_no", "buyer_tender_pushes", ["tender_no"]
    )
    op.create_index(
        "ix_buyer_tender_push_buyer_tender",
        "buyer_tender_pushes",
        ["buyer_user_id", "tender_no"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("buyer_tender_pushes")
