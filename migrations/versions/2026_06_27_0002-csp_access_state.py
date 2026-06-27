"""CSP access state — subscription_status + billing_type on csp_organisations

Revision ID: 2026_06_27_0002
Revises: 2026_06_27_0001
Create Date: 2026-06-27

Adds the access-gate columns the Stripe wiring needs. The CSP router blocks every
endpoint with HTTP 402 until ``subscription_status == 'active'``; the webhook flips
it on a paid purchase (``billing_type`` records whether that was a recurring
subscription or a one-time grant). Existing rows default to 'inactive'.
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_06_27_0002"
down_revision = "2026_06_27_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "csp_organisations",
        sa.Column("subscription_status", sa.String(20), server_default="'inactive'", nullable=True),
    )
    op.add_column(
        "csp_organisations",
        sa.Column("billing_type", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("csp_organisations", "billing_type")
    op.drop_column("csp_organisations", "subscription_status")
