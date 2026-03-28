"""V10 — TenderShortlist table for Tender Win Probability tool

Revision ID: v10_tender_shortlist
Revises: v10_marketplace
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers
revision = "v10_tender_shortlist"
down_revision = "v10_marketplace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tender_shortlists",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("tender_no", sa.String(100), nullable=False, unique=True),
        sa.Column("sector", sa.String(100), nullable=False),
        sa.Column("agency", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_rate", sa.Float(), nullable=False, server_default="0.20"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_tender_shortlists_tender_no", "tender_shortlists", ["tender_no"], unique=True)
    op.create_index("ix_tender_shortlists_sector", "tender_shortlists", ["sector"])
    op.create_index("ix_tender_shortlists_agency", "tender_shortlists", ["agency"])
    op.create_index("ix_tender_shortlists_sector_agency", "tender_shortlists", ["sector", "agency"])


def downgrade() -> None:
    op.drop_index("ix_tender_shortlists_sector_agency", "tender_shortlists")
    op.drop_index("ix_tender_shortlists_agency", "tender_shortlists")
    op.drop_index("ix_tender_shortlists_sector", "tender_shortlists")
    op.drop_index("ix_tender_shortlists_tender_no", "tender_shortlists")
    op.drop_table("tender_shortlists")
