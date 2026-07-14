"""add email_suppressions table

Revision ID: 2026_07_14_0001
Revises: 2026_07_03_0002
Create Date: 2026-07-14

Bounce/complaint (SES SNS) and one-click List-Unsubscribe now write to a
suppression table that `send_html_email` consults before every dispatch.
`scope="all"` (bounce/complaint) blocks every send; `scope="marketing"`
(unsubscribe) blocks only recurring/marketing sends.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_07_14_0001"
down_revision = "2026_07_03_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_suppressions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "scope",
            sa.String(length=20),
            nullable=False,
            server_default="all",
        ),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_email_suppressions_email",
        "email_suppressions",
        ["email"],
    )
    op.create_index(
        "ux_email_suppressions_email_scope",
        "email_suppressions",
        ["email", "scope"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_email_suppressions_email_scope", table_name="email_suppressions")
    op.drop_index("ix_email_suppressions_email", table_name="email_suppressions")
    op.drop_table("email_suppressions")
