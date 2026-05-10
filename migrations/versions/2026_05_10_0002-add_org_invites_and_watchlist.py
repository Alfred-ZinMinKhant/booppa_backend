"""add organisation invites and shared vendor watchlist tables

Revision ID: 2026_05_10_0002
Revises: 2026_05_10_0001
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_05_10_0002"
down_revision = "2026_05_10_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organisation_invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", UUID(as_uuid=True),
                  sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("role", sa.String(50), server_default="member"),
        sa.Column("token", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("invited_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("organisation_id", "email", name="uq_org_invite_email"),
    )

    op.create_table(
        "vendor_watchlist_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", UUID(as_uuid=True),
                  sa.ForeignKey("organisations.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("vendor_ref", sa.String(255), nullable=False),
        sa.Column("vendor_name", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("added_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("organisation_id", "vendor_ref", name="uq_watchlist_org_vendor"),
    )

    op.create_table(
        "vendor_watchlist_comments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("watchlist_item_id", UUID(as_uuid=True),
                  sa.ForeignKey("vendor_watchlist_items.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("author_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("vendor_watchlist_comments")
    op.drop_table("vendor_watchlist_items")
    op.drop_table("organisation_invites")
