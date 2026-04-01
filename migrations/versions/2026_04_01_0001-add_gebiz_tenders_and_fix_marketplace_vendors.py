"""Add gebiz_tenders table and fix marketplace_vendors schema

Revision ID: 2026_04_01_0001
Revises: v10_tender_shortlist
Create Date: 2026-04-01

Changes:
- Create gebiz_tenders table (missing from previous migrations)
- Rename marketplace_vendors.seo_slug -> slug
- Drop stale columns from marketplace_vendors (entity_type, registration_date,
  logo_url, claimed, claimed_by, trust_score, tier, discovery_source)
- Add missing columns to marketplace_vendors (linkedin_url, crunchbase_url,
  scan_status, scan_completed_at, claimed_by_user_id, claimed_at,
  import_batch_id, source)
- Add composite index ix_marketplace_vendors_industry_country
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB

revision = "2026_04_01_0001"
down_revision = "v10_tender_shortlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── gebiz_tenders ─────────────────────────────────────────────────────────
    op.create_table(
        "gebiz_tenders",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("tender_no", sa.String(100), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("agency", sa.String(255), nullable=False),
        sa.Column("closing_date", sa.DateTime(), nullable=True),
        sa.Column("estimated_value", sa.Float(), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="Open"),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("raw_data", JSONB(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_gebiz_tenders_tender_no", "gebiz_tenders", ["tender_no"], unique=True)
    op.create_index("ix_gebiz_tenders_agency", "gebiz_tenders", ["agency"])
    op.create_index("ix_gebiz_tenders_closing_date", "gebiz_tenders", ["closing_date"])
    op.create_index("ix_gebiz_tenders_status", "gebiz_tenders", ["status"])
    op.create_index("ix_gebiz_tenders_status_closing", "gebiz_tenders", ["status", "closing_date"])

    # ── marketplace_vendors: rename seo_slug -> slug ───────────────────────────
    op.alter_column("marketplace_vendors", "seo_slug", new_column_name="slug")

    # Drop stale columns no longer in the model
    op.drop_column("marketplace_vendors", "entity_type")
    op.drop_column("marketplace_vendors", "registration_date")
    op.drop_column("marketplace_vendors", "logo_url")
    op.drop_column("marketplace_vendors", "claimed")
    op.drop_column("marketplace_vendors", "claimed_by")
    op.drop_column("marketplace_vendors", "trust_score")
    op.drop_column("marketplace_vendors", "tier")
    op.drop_column("marketplace_vendors", "discovery_source")

    # Add columns present in model but missing from DB
    op.add_column("marketplace_vendors", sa.Column("linkedin_url", sa.String(500), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("crunchbase_url", sa.String(500), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("scan_status", sa.String(20), nullable=False, server_default="NONE"))
    op.add_column("marketplace_vendors", sa.Column("scan_completed_at", sa.DateTime(), nullable=True))
    op.add_column("marketplace_vendors", sa.Column(
        "claimed_by_user_id", PG_UUID(as_uuid=True), nullable=True
    ))
    op.add_column("marketplace_vendors", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("import_batch_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("source", sa.String(50), nullable=False, server_default="csv"))

    # Add missing indexes
    op.create_index("ix_marketplace_vendors_company_name", "marketplace_vendors", ["company_name"])
    op.create_index("ix_marketplace_vendors_domain", "marketplace_vendors", ["domain"])
    op.create_index("ix_marketplace_vendors_uen", "marketplace_vendors", ["uen"])
    op.create_index("ix_marketplace_vendors_scan_status", "marketplace_vendors", ["scan_status"])
    op.create_index("ix_marketplace_vendors_claimed_by_user_id", "marketplace_vendors", ["claimed_by_user_id"])
    op.create_index("ix_marketplace_vendors_import_batch_id", "marketplace_vendors", ["import_batch_id"])
    op.create_index("ix_marketplace_vendors_created_at", "marketplace_vendors", ["created_at"])
    op.create_index("ix_marketplace_vendors_industry_country", "marketplace_vendors", ["industry", "country"])

    # Add FK constraint for claimed_by_user_id
    op.create_foreign_key(
        "fk_marketplace_vendors_claimed_by_user_id",
        "marketplace_vendors", "users",
        ["claimed_by_user_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_marketplace_vendors_claimed_by_user_id", "marketplace_vendors", type_="foreignkey")

    op.drop_index("ix_marketplace_vendors_industry_country", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_created_at", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_import_batch_id", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_claimed_by_user_id", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_scan_status", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_uen", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_domain", "marketplace_vendors")
    op.drop_index("ix_marketplace_vendors_company_name", "marketplace_vendors")

    op.drop_column("marketplace_vendors", "source")
    op.drop_column("marketplace_vendors", "import_batch_id")
    op.drop_column("marketplace_vendors", "claimed_at")
    op.drop_column("marketplace_vendors", "claimed_by_user_id")
    op.drop_column("marketplace_vendors", "scan_completed_at")
    op.drop_column("marketplace_vendors", "scan_status")
    op.drop_column("marketplace_vendors", "crunchbase_url")
    op.drop_column("marketplace_vendors", "linkedin_url")

    op.add_column("marketplace_vendors", sa.Column("discovery_source", sa.String(50), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("tier", sa.String(20), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("trust_score", sa.Integer(), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("claimed_by", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("claimed", sa.Boolean(), server_default="false"))
    op.add_column("marketplace_vendors", sa.Column("logo_url", sa.String(500), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("registration_date", sa.String(30), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("entity_type", sa.String(100), nullable=True))

    op.alter_column("marketplace_vendors", "slug", new_column_name="seo_slug")

    op.drop_index("ix_gebiz_tenders_status_closing", "gebiz_tenders")
    op.drop_index("ix_gebiz_tenders_status", "gebiz_tenders")
    op.drop_index("ix_gebiz_tenders_closing_date", "gebiz_tenders")
    op.drop_index("ix_gebiz_tenders_agency", "gebiz_tenders")
    op.drop_index("ix_gebiz_tenders_tender_no", "gebiz_tenders")
    op.drop_table("gebiz_tenders")
