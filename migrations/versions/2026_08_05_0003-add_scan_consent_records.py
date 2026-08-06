"""add scan_consent_records (component 6.8 — explicit consent)

Revision ID: 2026_08_05_0003
Revises: 2026_08_05_0002
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_08_05_0003"
down_revision = "2026_08_05_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_consent_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("csp_client_id", UUID(as_uuid=True), sa.ForeignKey("csp_clients.id"), nullable=True),
        sa.Column("managed_entity_id", UUID(as_uuid=True), sa.ForeignKey("managed_entities.id"), nullable=True),
        sa.Column("collected_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("consent_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("consent_text_shown", sa.Text(), nullable=False),
        sa.Column("consent_acra_lookup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_domain_scan", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_address_screening", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_ubo_graph", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("blockchain_tx_hash", sa.String(80), nullable=True),
        sa.Column("polygonscan_url", sa.String(500), nullable=True),
        sa.Column("notarized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_scan_consent_csp_client", "scan_consent_records", ["csp_client_id"])
    op.create_index("ix_scan_consent_managed_entity", "scan_consent_records", ["managed_entity_id"])
    op.create_index(
        "ix_scan_consent_active", "scan_consent_records",
        ["csp_client_id", "managed_entity_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scan_consent_active", table_name="scan_consent_records")
    op.drop_index("ix_scan_consent_managed_entity", table_name="scan_consent_records")
    op.drop_index("ix_scan_consent_csp_client", table_name="scan_consent_records")
    op.drop_table("scan_consent_records")
