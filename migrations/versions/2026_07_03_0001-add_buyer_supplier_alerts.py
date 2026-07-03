"""Buyer supplier drift-alert dedup ledger

Revision ID: 2026_07_03_0001
Revises: 2026_07_02_0001
Create Date: 2026-07-03

One row per (buyer, watched supplier). Records the last state we alerted the buyer
about so the event-triggered drift sweep only emails on a *new* material change
(score drop / flip to FLAGGED|CRITICAL / approaching cert expiry) instead of
re-sending every run.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_07_03_0001"
down_revision = "2026_07_02_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buyer_supplier_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "buyer_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor_ref", sa.String(255), nullable=False),
        sa.Column("last_trust_score", sa.Integer(), nullable=True),
        sa.Column("last_risk_signal", sa.String(50), nullable=True),
        sa.Column("last_expiry_warned_for", sa.DateTime(), nullable=True),
        sa.Column("last_reason", sa.String(64), nullable=True),
        sa.Column("last_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_buyer_supplier_alerts_buyer_user_id", "buyer_supplier_alerts", ["buyer_user_id"]
    )
    op.create_index(
        "ix_buyer_supplier_alerts_vendor_ref", "buyer_supplier_alerts", ["vendor_ref"]
    )
    op.create_index(
        "ix_buyer_supplier_alert_buyer_ref",
        "buyer_supplier_alerts",
        ["buyer_user_id", "vendor_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("buyer_supplier_alerts")
