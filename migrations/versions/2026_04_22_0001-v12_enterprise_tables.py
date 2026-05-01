"""v12 enterprise tables

Revision ID: 2026_04_22_0001
Revises: 2026_05_01_0001
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2026_04_22_0001"
down_revision = "2026_05_01_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), unique=True, nullable=False),
        sa.Column("tier", sa.String(50), server_default="'standard'"),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("idx_organisations_owner", "organisations", ["owner_user_id"])

    op.create_table(
        "subsidiaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("uen", sa.String(50)),
        sa.Column("country", sa.String(100), server_default="'Singapore'"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "organisation_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(50), server_default="'member'"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.UniqueConstraint("organisation_id", "user_id", name="uq_org_members"),
    )

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column("events", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("webhook_endpoints.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("status_code", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column("success", sa.Boolean(), server_default="false"),
        sa.Column("attempt", sa.Integer(), server_default="1"),
        sa.Column("delivered_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("idx_webhook_deliveries_endpoint", "webhook_deliveries", ["endpoint_id"])

    op.create_table(
        "trm_controls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("domain", sa.String(100), nullable=False),
        sa.Column("control_ref", sa.String(50)),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(30), server_default="'not_started'"),
        sa.Column("gap_analysis", sa.Text()),
        sa.Column("risk_rating", sa.String(20)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("idx_trm_controls_org", "trm_controls", ["organisation_id"])

    op.create_table(
        "trm_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("control_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trm_controls.id"), nullable=False),
        sa.Column("file_name", sa.String(255)),
        sa.Column("s3_key", sa.Text()),
        sa.Column("hash_value", sa.String(64)),
        sa.Column("tx_hash", sa.String(66)),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "retention_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("data_category", sa.String(100), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("auto_purge", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "sso_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), unique=True, nullable=False),
        sa.Column("protocol", sa.String(20), nullable=False),
        sa.Column("idp_metadata_url", sa.Text()),
        sa.Column("idp_entity_id", sa.Text()),
        sa.Column("sp_acs_url", sa.Text()),
        sa.Column("client_id", sa.String(255)),
        sa.Column("client_secret", sa.String(255)),
        sa.Column("discovery_url", sa.Text()),
        sa.Column("is_active", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "white_label_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), unique=True, nullable=False),
        sa.Column("logo_s3_key", sa.Text()),
        sa.Column("primary_color", sa.String(7), server_default="'#10b981'"),
        sa.Column("secondary_color", sa.String(7), server_default="'#0f172a'"),
        sa.Column("footer_text", sa.Text()),
        sa.Column("report_header_text", sa.Text()),
        sa.Column("custom_domain", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    op.create_table(
        "sla_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("target_minutes", sa.Integer()),
        sa.Column("actual_minutes", sa.Integer()),
        sa.Column("met", sa.Boolean()),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("idx_sla_logs_org", "sla_logs", ["organisation_id"])


def downgrade() -> None:
    op.drop_table("sla_logs")
    op.drop_table("white_label_configs")
    op.drop_table("sso_configs")
    op.drop_table("retention_policies")
    op.drop_table("trm_evidence")
    op.drop_table("trm_controls")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
    op.drop_table("organisation_members")
    op.drop_table("subsidiaries")
    op.drop_table("organisations")
