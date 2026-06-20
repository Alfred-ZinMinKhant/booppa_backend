"""add scan_evidence column to evidence_packs

Revision ID: 2026_06_20_0003
Revises: 2026_06_20_0002
Create Date: 2026-06-20

Stores the observed website/PDPA-scan signals used to ground the BCEP documents
(so generation reflects real evidence, not just the intake form). Nullable JSONB —
existing rows and intake-only generation are unaffected.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "2026_06_20_0003"
down_revision = "2026_06_20_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_packs",
        sa.Column("scan_evidence", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidence_packs", "scan_evidence")
