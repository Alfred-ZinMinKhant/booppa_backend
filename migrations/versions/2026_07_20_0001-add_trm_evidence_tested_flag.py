"""add tested-vs-documented flag to trm_evidence

Revision ID: 2026_07_20_0001
Revises: 2026_07_18_0001
Create Date: 2026-07-20

MAS treats an untested control/plan as "an aspiration, not a control". This adds
the columns that let a piece of TRM evidence declare itself as tested (e.g. an
annual DR test) rather than merely documented, so the TRM Baseline can render an
inspection-defensible distinction:

- evidence_type: 'documented' (default) | 'tested'
- tested_at:     date the control/plan was last tested
- attestation:   short "what was tested / by whom" note

All additive & nullable/defaulted — safe to apply on a live table.
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_07_20_0001"
down_revision = "2026_07_18_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trm_evidence",
        sa.Column("evidence_type", sa.String(20), nullable=True, server_default="documented"),
    )
    op.add_column("trm_evidence", sa.Column("tested_at", sa.DateTime, nullable=True))
    op.add_column("trm_evidence", sa.Column("attestation", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("trm_evidence", "attestation")
    op.drop_column("trm_evidence", "tested_at")
    op.drop_column("trm_evidence", "evidence_type")
