"""Add demo bookings

Revision ID: 3d9a4f9c2a91
Revises: 0dd4e428633f
Create Date: 2026-01-27 12:00:00.000000+00:00

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3d9a4f9c2a91"
down_revision = "0dd4e428633f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_bookings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slot_id", sa.String(length=32), nullable=False),
        sa.Column("slot_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.String(length=5), nullable=False),
        sa.Column("end_time", sa.String(length=5), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("customer_email", sa.String(length=255), nullable=False),
        sa.Column("customer_phone", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("booking_token", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_demo_bookings_booking_token"),
        "demo_bookings",
        ["booking_token"],
        unique=True,
    )
    op.create_index(
        op.f("ix_demo_bookings_created_at"),
        "demo_bookings",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demo_bookings_customer_email"),
        "demo_bookings",
        ["customer_email"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demo_bookings_slot_date"),
        "demo_bookings",
        ["slot_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demo_bookings_slot_id"),
        "demo_bookings",
        ["slot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demo_bookings_status"),
        "demo_bookings",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_demo_bookings_status"), table_name="demo_bookings")
    op.drop_index(op.f("ix_demo_bookings_slot_id"), table_name="demo_bookings")
    op.drop_index(op.f("ix_demo_bookings_slot_date"), table_name="demo_bookings")
    op.drop_index(
        op.f("ix_demo_bookings_customer_email"), table_name="demo_bookings"
    )
    op.drop_index(
        op.f("ix_demo_bookings_created_at"), table_name="demo_bookings"
    )
    op.drop_index(
        op.f("ix_demo_bookings_booking_token"), table_name="demo_bookings"
    )
    op.drop_table("demo_bookings")