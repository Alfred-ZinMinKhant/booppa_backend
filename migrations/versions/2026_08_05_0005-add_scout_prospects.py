"""add scout_prospects (SCOUT agents persistence)

Revision ID: 2026_08_05_0005
Revises: 2026_08_05_0004
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "2026_08_05_0005"
down_revision = "2026_08_05_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scout_prospects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("pipeline", sa.String(20), nullable=False),
        sa.Column("natural_key", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("uen", sa.String(20), nullable=True),
        sa.Column("website_url", sa.String(500), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("priority_tier", sa.String(10), nullable=True),
        sa.Column("fit_tier", sa.String(40), nullable=True),
        sa.Column("raw_data", JSONB(), nullable=True),
        sa.Column("outreach_subject", sa.String(255), nullable=True),
        sa.Column("outreach_body_html", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="NEW"),
        sa.Column("status_reason", sa.String(500), nullable=True),
        sa.Column("first_scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("pipeline", "natural_key", name="uq_scout_prospect_pipeline_key"),
    )
    op.create_index("ix_scout_prospect_pipeline", "scout_prospects", ["pipeline"])
    op.create_index("ix_scout_prospect_natural_key", "scout_prospects", ["natural_key"])
    op.create_index("ix_scout_prospect_uen", "scout_prospects", ["uen"])
    op.create_index("ix_scout_prospect_priority_tier", "scout_prospects", ["priority_tier"])
    op.create_index("ix_scout_prospect_status", "scout_prospects", ["status"])
    op.create_index("ix_scout_prospect_status_pipeline", "scout_prospects", ["status", "pipeline"])
    op.create_index("ix_scout_prospect_tier_status", "scout_prospects", ["priority_tier", "status"])


def downgrade() -> None:
    op.drop_index("ix_scout_prospect_tier_status", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_status_pipeline", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_status", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_priority_tier", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_uen", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_natural_key", table_name="scout_prospects")
    op.drop_index("ix_scout_prospect_pipeline", table_name="scout_prospects")
    op.drop_table("scout_prospects")
