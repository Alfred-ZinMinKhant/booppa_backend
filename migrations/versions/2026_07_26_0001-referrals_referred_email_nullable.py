"""make referrals.referred_email nullable

Revision ID: 2026_07_26_0001
Revises: 2026_07_25_0001
Create Date: 2026-07-26

`Referral.referred_email` is declared `nullable=True` on the model and the
`POST /referrals/create` request body types it `Optional[str] = None` (a vendor
can mint a shareable code before knowing who they're inviting), but the original
v10 marketplace migration created the column NOT NULL. Every create call that
omitted an email therefore died on an IntegrityError and returned a 500.

Relaxing a NOT NULL is safe on a live table: existing rows already satisfy the
weaker constraint and no backfill is needed. The downgrade re-imposes NOT NULL
and will fail if NULL rows exist by then — deliberately, rather than inventing
a placeholder email for real referrals.
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_07_26_0001"
down_revision = "2026_07_25_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "referrals",
        "referred_email",
        existing_type=sa.String(255),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "referrals",
        "referred_email",
        existing_type=sa.String(255),
        nullable=False,
    )
