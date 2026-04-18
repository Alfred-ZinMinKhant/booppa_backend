"""add missing columns to funnel_events and referrals

Revision ID: 2026_04_18_0001
Revises: 2026_04_16_0002
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "2026_04_18_0001"
down_revision = "2026_04_16_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── funnel_events: add columns that the model defines but the original migration missed ──
    op.add_column("funnel_events", sa.Column("previous_stage", sa.String(50), nullable=True))
    op.add_column("funnel_events", sa.Column("utm_source", sa.String(100), nullable=True))
    op.add_column("funnel_events", sa.Column("utm_medium", sa.String(100), nullable=True))
    op.add_column("funnel_events", sa.Column("utm_campaign", sa.String(100), nullable=True))
    op.add_column("funnel_events", sa.Column("ip_address", sa.String(45), nullable=True))
    op.add_column("funnel_events", sa.Column("user_agent", sa.String(500), nullable=True))
    # Widen 'stage' from String(30) to String(50) to match model
    op.alter_column("funnel_events", "stage", type_=sa.String(50), existing_type=sa.String(30))
    # Add index on session_id (model has index=True)
    op.create_index("ix_funnel_events_session_id", "funnel_events", ["session_id"])
    op.create_index("ix_funnel_events_created_at", "funnel_events", ["created_at"])
    op.create_index("ix_funnel_events_user_id", "funnel_events", ["user_id"])

    # ── referrals: add columns that the model defines but the original migration missed ──
    op.add_column("referrals", sa.Column("referred_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("referrals", sa.Column("reward_type", sa.String(50), nullable=True))
    op.add_column("referrals", sa.Column("reward_amount_cents", sa.Integer(), server_default="0"))
    op.add_column("referrals", sa.Column("reward_claimed", sa.Boolean(), server_default="false"))
    op.add_column("referrals", sa.Column("reward_claimed_at", sa.DateTime(), nullable=True))
    op.add_column("referrals", sa.Column("converted_at", sa.DateTime(), nullable=True))
    op.add_column("referrals", sa.Column("expires_at", sa.DateTime(), nullable=True))
    # Add indexes
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"])
    op.create_index("ix_referrals_referred_id", "referrals", ["referred_id"])
    op.create_index("ix_referrals_status", "referrals", ["status"])


def downgrade() -> None:
    # referrals
    op.drop_index("ix_referrals_status")
    op.drop_index("ix_referrals_referred_id")
    op.drop_index("ix_referrals_referrer_id")
    op.drop_column("referrals", "expires_at")
    op.drop_column("referrals", "converted_at")
    op.drop_column("referrals", "reward_claimed_at")
    op.drop_column("referrals", "reward_claimed")
    op.drop_column("referrals", "reward_amount_cents")
    op.drop_column("referrals", "reward_type")
    op.drop_column("referrals", "referred_id")

    # funnel_events
    op.drop_index("ix_funnel_events_user_id")
    op.drop_index("ix_funnel_events_created_at")
    op.drop_index("ix_funnel_events_session_id")
    op.alter_column("funnel_events", "stage", type_=sa.String(30), existing_type=sa.String(50))
    op.drop_column("funnel_events", "user_agent")
    op.drop_column("funnel_events", "ip_address")
    op.drop_column("funnel_events", "utm_campaign")
    op.drop_column("funnel_events", "utm_medium")
    op.drop_column("funnel_events", "utm_source")
    op.drop_column("funnel_events", "previous_stage")
