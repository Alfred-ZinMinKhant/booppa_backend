"""add website_hint to users

Revision ID: 2026_07_27_0001
Revises: 2026_07_26_0003
Create Date: 2026-07-27

Companion to `legal_name_hint` (2026_07_25_0001). That column records the
company string a cached identity was resolved from; this one records the
website/domain it was resolved alongside.

Company strings collide (trading names, abbreviations) and several deliverables
render the domain itself as the assessed subject ("Assessed Entity: <domain>" on
the MAS TRM / CSP baselines). A purchase naming a different website is therefore
a different subject even when the company string matches, and must not reuse the
cached `legal_name`/`uen` — it re-resolves fresh instead.

Additive & nullable — safe on a live table. Existing rows get NULL, which the
resolver treats as unknown provenance (stale), so no backfill is required.
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_07_27_0001"
down_revision = "2026_07_26_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("website_hint", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "website_hint")
