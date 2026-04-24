"""add funnel_events.metadata + v11 compliance/supply-chain tables

Revision ID: 2026_04_25_0001
Revises: 2026_04_24_0001
Create Date: 2026-04-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2026_04_25_0001"
down_revision = "2026_04_24_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. funnel_events.metadata (missing column causing 500s) ───────────────
    op.add_column(
        "funnel_events",
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )

    # ── 2. compliance_requirements (V11) ──────────────────────────────────────
    op.create_table(
        "compliance_requirements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("regulation_key", sa.String(50), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required_frameworks", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("requires_notarization", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reference_url", sa.String(500), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true", index=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    # ── 3. managed_vendors (V11) ──────────────────────────────────────────────
    op.create_table(
        "managed_vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "enterprise_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "vendor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("vendor_name", sa.String(255), nullable=True),
        sa.Column("vendor_email", sa.String(255), nullable=True, index=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE", index=True),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("alert_threshold", sa.String(20), nullable=False, server_default="WATCH"),
        sa.Column("cached_risk_signal", sa.String(20), nullable=True),
        sa.Column("cached_verification_depth", sa.String(50), nullable=True),
        sa.Column("cached_procurement_readiness", sa.String(50), nullable=True),
        sa.Column("cached_total_score", sa.Integer(), nullable=True),
        sa.Column("cache_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("invite_token", sa.String(100), nullable=True, unique=True, index=True),
        sa.Column("invite_sent_at", sa.DateTime(), nullable=True),
        sa.Column("invite_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("vendor_compliance_score", sa.Float(), nullable=True),
        sa.Column("vendor_compliance_updated_at", sa.DateTime(), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "enterprise_user_id", "vendor_user_id",
            name="uq_managed_vendor_enterprise_vendor",
        ),
    )
    op.create_index(
        "ix_managed_vendors_enterprise_status",
        "managed_vendors",
        ["enterprise_user_id", "status"],
    )
    op.create_index(
        "ix_managed_vendors_enterprise_risk",
        "managed_vendors",
        ["enterprise_user_id", "cached_risk_signal"],
    )


def downgrade() -> None:
    op.drop_index("ix_managed_vendors_enterprise_risk", table_name="managed_vendors")
    op.drop_index("ix_managed_vendors_enterprise_status", table_name="managed_vendors")
    op.drop_table("managed_vendors")
    op.drop_table("compliance_requirements")
    op.drop_column("funnel_events", "metadata")
