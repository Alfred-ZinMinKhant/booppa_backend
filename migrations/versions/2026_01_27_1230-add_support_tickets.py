"""Add support tickets

Revision ID: 7a1f8b1f5b22
Revises: 3d9a4f9c2a91
Create Date: 2026-01-27 12:30:00.000000+00:00

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7a1f8b1f5b22"
down_revision = "3d9a4f9c2a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.String(length=50), nullable=False),
        sa.Column("tracking_token", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("priority", sa.String(length=32), nullable=True),
        sa.Column("assigned_to", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_support_tickets_ticket_id"),
        "support_tickets",
        ["ticket_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_support_tickets_tracking_token"),
        "support_tickets",
        ["tracking_token"],
        unique=True,
    )
    op.create_index(
        op.f("ix_support_tickets_email"),
        "support_tickets",
        ["email"],
        unique=False,
    )
    op.create_index(
        op.f("ix_support_tickets_status"),
        "support_tickets",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_support_tickets_priority"),
        "support_tickets",
        ["priority"],
        unique=False,
    )
    op.create_index(
        op.f("ix_support_tickets_created_at"),
        "support_tickets",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "support_ticket_replies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.String(length=50), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("author_type", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_support_ticket_replies_ticket_id"),
        "support_ticket_replies",
        ["ticket_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_support_ticket_replies_created_at"),
        "support_ticket_replies",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_support_ticket_replies_created_at"),
        table_name="support_ticket_replies",
    )
    op.drop_index(
        op.f("ix_support_ticket_replies_ticket_id"),
        table_name="support_ticket_replies",
    )
    op.drop_table("support_ticket_replies")

    op.drop_index(
        op.f("ix_support_tickets_created_at"),
        table_name="support_tickets",
    )
    op.drop_index(
        op.f("ix_support_tickets_priority"),
        table_name="support_tickets",
    )
    op.drop_index(
        op.f("ix_support_tickets_status"),
        table_name="support_tickets",
    )
    op.drop_index(
        op.f("ix_support_tickets_email"),
        table_name="support_tickets",
    )
    op.drop_index(
        op.f("ix_support_tickets_tracking_token"),
        table_name="support_tickets",
    )
    op.drop_index(
        op.f("ix_support_tickets_ticket_id"),
        table_name="support_tickets",
    )
    op.drop_table("support_tickets")