"""add notarization_credits balance to users

Revision ID: 2026_05_02_0001
Revises: 2026_04_22_0001
Create Date: 2026-05-02
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_05_02_0001"
down_revision = "2026_04_22_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notarization_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notarization_credits")
