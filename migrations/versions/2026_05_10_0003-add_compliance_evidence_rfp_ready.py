"""add compliance_evidence_rfp_ready flag to users

Revision ID: 2026_05_10_0003
Revises: 2026_05_10_0002
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_05_10_0003"
down_revision = "2026_05_10_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "compliance_evidence_rfp_ready",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "compliance_evidence_rfp_ready")
