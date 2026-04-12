"""v11 subscriptions: add subscription fields to users and processed_webhook_events table

Revision ID: 2026_04_12_0001
Revises: 2026_04_10_0001
Create Date: 2026-04-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "2026_04_12_0001"
down_revision = "2026_04_10_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add subscription fields to users
    op.add_column("users", sa.Column("subscription_tier", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("subscription_started_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255), nullable=True))

    # Idempotency guard for Stripe webhook events
    op.create_table(
        "processed_webhook_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_processed_webhook_events_event_id", "processed_webhook_events", ["event_id"], unique=True)
    op.create_index("ix_processed_webhook_events_processed_at", "processed_webhook_events", ["processed_at"])


def downgrade() -> None:
    op.drop_index("ix_processed_webhook_events_processed_at", table_name="processed_webhook_events")
    op.drop_index("ix_processed_webhook_events_event_id", table_name="processed_webhook_events")
    op.drop_table("processed_webhook_events")
    op.drop_column("users", "stripe_customer_id")
    op.drop_column("users", "subscription_started_at")
    op.drop_column("users", "subscription_tier")
