"""add buyer_name_usage_authorizations and buyer_cascade_notification_log

Revision ID: 2026_08_05_0004
Revises: 2026_08_05_0003
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_08_05_0004"
down_revision = "2026_08_05_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buyer_name_usage_authorizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("buyer_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("organisation_id", UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=True),
        sa.Column("authorization_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("clause_text_shown", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("blockchain_tx_hash", sa.String(80), nullable=True),
        sa.Column("polygonscan_url", sa.String(500), nullable=True),
        sa.Column("notarized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_buyer_name_auth_active", "buyer_name_usage_authorizations", ["buyer_user_id", "revoked_at"])

    op.create_table(
        "buyer_cascade_notification_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("buyer_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("vendor_email", sa.String(255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_buyer_cascade_log_buyer", "buyer_cascade_notification_log", ["buyer_user_id"])
    op.create_index("ix_buyer_cascade_log_email", "buyer_cascade_notification_log", ["vendor_email"])
    op.create_index("ix_buyer_cascade_log_cooldown", "buyer_cascade_notification_log", ["buyer_user_id", "vendor_email", "sent_at"])


def downgrade() -> None:
    op.drop_table("buyer_cascade_notification_log")
    op.drop_table("buyer_name_usage_authorizations")
