"""add api_keys table and parent_user_id column

Revision ID: 2026_05_17_0001
Revises: 2026_05_13_0001
Create Date: 2026-05-17

Webhook/SSO/Organisation tables already exist (2026_04_22_0001-v12_enterprise_tables);
this migration only adds the new ApiKey table plus a self-FK on users for the
"add subsidiary" flow that attaches a user as a child of another.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_05_17_0001"
down_revision = "2026_05_13_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "parent_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_parent_user_id", "users", ["parent_user_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("hashed_key", sa.String(64), nullable=False, unique=True),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_user_active", "api_keys", ["user_id", "revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_user_active", table_name="api_keys")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_users_parent_user_id", table_name="users")
    op.drop_column("users", "parent_user_id")
