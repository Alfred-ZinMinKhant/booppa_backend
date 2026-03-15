"""V10 marketplace, feature flags, funnel analytics, leaderboard

Revision ID: v10_marketplace
Revises: (latest)
Create Date: 2026-03-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers
revision = "v10_marketplace"
down_revision = None  # will be set by alembic autogenerate
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── MarketplaceVendor ─────────────────────────────────────────────────
    op.create_table(
        "marketplace_vendors",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("seo_slug", sa.String(300), nullable=False, unique=True),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("industry", sa.String(100), nullable=True),
        sa.Column("country", sa.String(100), server_default="Singapore"),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("uen", sa.String(20), nullable=True, unique=True),
        sa.Column("entity_type", sa.String(100), nullable=True),
        sa.Column("registration_date", sa.String(30), nullable=True),
        sa.Column("logo_url", sa.String(500), nullable=True),
        sa.Column("claimed", sa.Boolean(), server_default="false"),
        sa.Column("claimed_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("trust_score", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(20), nullable=True),
        sa.Column("discovery_source", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_marketplace_vendors_industry", "marketplace_vendors", ["industry"])
    op.create_index("ix_marketplace_vendors_country", "marketplace_vendors", ["country"])
    op.create_index("ix_marketplace_vendors_tier", "marketplace_vendors", ["tier"])

    # ── DiscoveredVendor ──────────────────────────────────────────────────
    op.create_table(
        "discovered_vendors",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True, unique=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("scan_status", sa.String(20), server_default="SCANNING"),
        sa.Column("claim_token", sa.String(100), nullable=True),
        sa.Column("claimed_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── ImportBatch ───────────────────────────────────────────────────────
    op.create_table(
        "import_batches",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("total_rows", sa.Integer(), server_default="0"),
        sa.Column("inserted", sa.Integer(), server_default="0"),
        sa.Column("updated", sa.Integer(), server_default="0"),
        sa.Column("skipped", sa.Integer(), server_default="0"),
        sa.Column("errors", sa.Integer(), server_default="0"),
        sa.Column("imported_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── FunnelEvent ───────────────────────────────────────────────────────
    op.create_table(
        "funnel_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", sa.String(100), nullable=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_funnel_events_stage", "funnel_events", ["stage"])

    # ── RevenueEvent ──────────────────────────────────────────────────────
    op.create_table(
        "revenue_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("revenue_type", sa.String(30), nullable=False),
        sa.Column("product", sa.String(100), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(10), server_default="SGD"),
        sa.Column("stripe_payment_id", sa.String(255), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_revenue_events_revenue_type", "revenue_events", ["revenue_type"])

    # ── SubscriptionSnapshot ──────────────────────────────────────────────
    op.create_table(
        "subscription_snapshots",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("total_mrr_cents", sa.Integer(), server_default="0"),
        sa.Column("active_subscriptions", sa.Integer(), server_default="0"),
        sa.Column("new_subscriptions", sa.Integer(), server_default="0"),
        sa.Column("churned", sa.Integer(), server_default="0"),
        sa.Column("expansion_cents", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_subscription_snapshots_month", "subscription_snapshots", ["month"], unique=True)

    # ── QuarterlyLeaderboard ──────────────────────────────────────────────
    op.create_table(
        "quarterly_leaderboards",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("quarter", sa.String(7), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=True),
        sa.Column("score", sa.Integer(), server_default="0"),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(20), nullable=True),
        sa.Column("badge_url", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_quarterly_leaderboards_quarter", "quarterly_leaderboards", ["quarter"])

    # ── Achievement ───────────────────────────────────────────────────────
    op.create_table(
        "achievements",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("achievement_type", sa.String(50), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.String(500), nullable=True),
        sa.Column("awarded_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── ScoreMilestone ────────────────────────────────────────────────────
    op.create_table(
        "score_milestones",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("milestone_type", sa.String(30), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("reached_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── PrestigeSlot ──────────────────────────────────────────────────────
    op.create_table(
        "prestige_slots",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("slot_name", sa.String(50), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=True),
        sa.Column("quarter", sa.String(7), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── Referral ──────────────────────────────────────────────────────────
    op.create_table(
        "referrals",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("referrer_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("referred_email", sa.String(255), nullable=False),
        sa.Column("referral_code", sa.String(50), nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="PENDING"),
        sa.Column("redeemed_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("reward_cents", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
    )

    # ── EnterpriseInviteToken ─────────────────────────────────────────────
    op.create_table(
        "enterprise_invite_tokens",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("token", sa.String(200), nullable=False, unique=True),
        sa.Column("enterprise_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("role", sa.String(50), server_default="member"),
        sa.Column("used", sa.Boolean(), server_default="false"),
        sa.Column("used_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── ApiUsage ──────────────────────────────────────────────────────────
    op.create_table(
        "api_usage",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("endpoint", sa.String(200), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_api_usage_user_id", "api_usage", ["user_id"])
    op.create_index("ix_api_usage_endpoint", "api_usage", ["endpoint"])

    # ── CertificateLog ────────────────────────────────────────────────────
    op.create_table(
        "certificate_logs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("certificate_type", sa.String(50), nullable=False),
        sa.Column("report_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("file_url", sa.String(500), nullable=True),
        sa.Column("blockchain_tx", sa.String(100), nullable=True),
        sa.Column("issued_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── FeatureFlag ───────────────────────────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("flag_name", sa.String(100), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), server_default="false"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("auto_activate_threshold", sa.Integer(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # ── EvidencePackage.tier (v12 bug fix) ────────────────────────────────
    op.add_column("evidence_packages", sa.Column("tier", sa.String(20), nullable=True))
    op.create_index("ix_evidence_packages_tier", "evidence_packages", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_evidence_packages_tier", table_name="evidence_packages")
    op.drop_column("evidence_packages", "tier")

    op.drop_table("feature_flags")
    op.drop_table("certificate_logs")
    op.drop_table("api_usage")
    op.drop_table("enterprise_invite_tokens")
    op.drop_table("referrals")
    op.drop_table("prestige_slots")
    op.drop_table("score_milestones")
    op.drop_table("achievements")
    op.drop_table("quarterly_leaderboards")
    op.drop_table("subscription_snapshots")
    op.drop_table("revenue_events")
    op.drop_table("funnel_events")
    op.drop_table("import_batches")
    op.drop_table("discovered_vendors")
    op.drop_table("marketplace_vendors")
