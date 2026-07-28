"""add identity provenance to discovered_vendors

Revision ID: 2026_07_28_0001
Revises: 2026_07_27_0001
Create Date: 2026-07-28

`User.legal_name_hint` / `User.website_hint` (2026_07_25_0001, 2026_07_27_0001)
made the account's cached identity safe to reuse. But an account is only ever the
right subject for a vendor self-assessing. A BUYER scans many different vendors
from one login, so the report's subject must live on the vendor row, not on the
buyer's account — otherwise resolution either falls back to the buyer's own
company (the SPQR leak) or refuses to resolve at all.

`discovered_vendors` is where a scanned vendor's identity belongs: it is already
keyed on `uen` and decoupled from `users`. These two columns give it the same
provenance contract the account rows have, so the one trust code path in
`evidence_enricher` can serve both (see `VENDOR_CARRIER`).

Additive & nullable — safe on a live table. Existing rows get NULL, which the
resolver treats as unknown provenance (untrusted) and re-resolves, so no backfill
is required.
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_07_28_0001"
down_revision = "2026_07_27_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovered_vendors", sa.Column("verified_hint", sa.String(255), nullable=True)
    )
    op.add_column(
        "discovered_vendors", sa.Column("verified_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("discovered_vendors", "verified_at")
    op.drop_column("discovered_vendors", "verified_hint")
