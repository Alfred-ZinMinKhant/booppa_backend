"""add legal_name_hint to users

Revision ID: 2026_07_25_0001
Revises: 2026_07_20_0002
Create Date: 2026-07-25

Records the exact `company` hint string that `legal_name` was last resolved
from (evidence_enricher.resolve_legal_name). The resolver trusts a cached
`legal_name`/`uen` only when the current purchase's company hint matches this
value; on divergence it re-resolves fresh. This closes the sticky-identity
leak where a reused account (or a customer whose company changed) kept
stamping a previously cached entity — e.g. "SPQR Communications" leaking into
an RFP kit purchased for a different vendor.

Additive & nullable — safe on a live table. Existing rows get NULL, which the
resolver treats as unknown provenance (stale) and re-resolves on next use, so
no backfill is required.
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_07_25_0001"
down_revision = "2026_07_20_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("legal_name_hint", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "legal_name_hint")
