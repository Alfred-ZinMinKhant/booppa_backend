"""add legal_name to users

Revision ID: 2026_07_20_0002
Revises: 2026_07_20_0001
Create Date: 2026-07-20

Canonical, ACRA-registered legal entity name — resolved via
evidence_enricher.resolve_legal_name. Distinct from `company` (raw user
input, e.g. a domain or trading name). Kills the recurring "Assessed
Entity: thunes.com" bug class by giving every PDF generator one shared
field to read instead of independently guessing a display name.

Additive & nullable — safe to apply on a live table. Existing rows are
backfilled by scripts/backfill_legal_names.py, not by this migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_07_20_0002"
down_revision = "2026_07_20_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("legal_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "legal_name")
