"""add organisations.mas_licence_type

The MAS TRM Document Pack must branch the Outsourcing Risk Register between two
disjoint regimes: Notices 658/1121 (binding on banks and merchant banks since
11 Dec 2024, with a MAS-published register template) and the Guidelines on
Outsourcing (Financial Institutions other than Banks). Nothing in the schema
could tell the two apart — `organisations.sector` collapses `bank`, `banking`,
`finance` and `payments` into one `fintech` bucket via
`trm_sector_override._SECTOR_ALIASES`.

**No backfill, deliberately.** `users.industry` is free text and cannot
distinguish a bank from a Major Payment Institution, and the UEN cannot either
(ACRA's open dataset carries no SSIC/activity field). Any backfill would
manufacture a regulatory classification the customer never gave us, which is
the precise failure this column exists to prevent. Existing organisations stay
NULL until the customer confirms; the pack generator gates the register on NULL
rather than defaulting.

Revision ID: 2026_08_03_0001
Revises: 2026_07_29_0002
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_08_03_0001"
down_revision = "2026_07_29_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("mas_licence_type", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organisations", "mas_licence_type")
