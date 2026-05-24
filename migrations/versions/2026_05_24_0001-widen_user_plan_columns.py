"""widen user.plan / subscription_tier to VARCHAR(50)

Revision ID: 2026_05_24_0001
Revises: 2026_05_23_0002
Create Date: 2026-05-24

The longest plan key, `verify_supplier_evidence` (24 chars), overflows the
original VARCHAR(20). The subscription webhook would commit fail (StringDataRightTruncation)
and roll back the whole activation, leaving paid customers on `plan='free'`.
Widen to VARCHAR(50) to leave headroom for future tier names.
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_05_24_0001"
down_revision = "2026_05_23_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "plan",
        existing_type=sa.String(length=20),
        type_=sa.String(length=50),
        existing_nullable=False,
        existing_server_default=sa.text("'free'::character varying"),
    )
    # Note: subscription_tier was already widened to VARCHAR(50) in an earlier
    # migration; we only need to widen `plan` here.


def downgrade() -> None:
    op.alter_column(
        "users",
        "plan",
        existing_type=sa.String(length=50),
        type_=sa.String(length=20),
        existing_nullable=False,
        existing_server_default=sa.text("'free'::character varying"),
    )
