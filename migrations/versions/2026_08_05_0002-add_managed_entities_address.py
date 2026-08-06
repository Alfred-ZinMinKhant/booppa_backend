"""add registered_address to managed_entities (Buyer-side address-screen support)

Revision ID: 2026_08_05_0002
Revises: 2026_08_05_0001
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_08_05_0002"
down_revision = "2026_08_05_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_entities",
        sa.Column("registered_address", sa.Text(), nullable=True),
    )
    op.add_column(
        "managed_entities",
        sa.Column("registered_address_source", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("managed_entities", "registered_address_source")
    op.drop_column("managed_entities", "registered_address")
