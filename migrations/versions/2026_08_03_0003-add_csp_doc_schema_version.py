"""add schema_version to csp_aml_programme

CSP documents are written by the model from prompt text, so grepping the repo
tells you nothing about which legal wording a PDF delivered last month was
produced under. The stamp is the only reliable marker.

Deliberately nullable with no server default and no backfill: existing rows
predate the constant, and the readers treat NULL as v1 — which is exactly what
those rows are. Backfilling them to the current version would silently assert
that already-delivered packs carry the corrected CDSA s.48 citation when they
carry s.48A.

Revision ID: 2026_08_03_0003
Revises: 2026_08_03_0002
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_08_03_0003"
down_revision = "2026_08_03_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "csp_aml_programme",
        sa.Column("schema_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("csp_aml_programme", "schema_version")
