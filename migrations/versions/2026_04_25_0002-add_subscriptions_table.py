"""create subscriptions table

Revision ID: 2026_04_25_0002
Revises: 2026_04_25_0001
Create Date: 2026-04-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2026_04_25_0002"
down_revision = "2026_04_25_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=False),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("product_type", sa.String(100), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_subscriptions_stripe_subscription_id ON subscriptions (stripe_subscription_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_stripe_customer_id ON subscriptions (stripe_customer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id ON subscriptions (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions (status)"
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_stripe_customer_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_stripe_subscription_id", table_name="subscriptions")
    # DROP INDEX IF EXISTS is supported via op.execute
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_status")
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_user_id")
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_stripe_customer_id")
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_stripe_subscription_id")
    op.drop_table("subscriptions")
