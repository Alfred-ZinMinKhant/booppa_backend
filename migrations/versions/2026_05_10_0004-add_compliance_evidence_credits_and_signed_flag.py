"""add compliance_evidence_credits + signed_cover_sheet_uploaded to users

Revision ID: 2026_05_10_0004
Revises: 2026_05_10_0003
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_05_10_0004"
down_revision = "2026_05_10_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "compliance_evidence_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "signed_cover_sheet_uploaded",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "signed_cover_sheet_uploaded")
    op.drop_column("users", "compliance_evidence_credits")
