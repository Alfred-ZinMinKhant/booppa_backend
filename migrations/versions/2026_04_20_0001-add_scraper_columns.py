"""add contact_email, scraped_data, last_scraped_at to marketplace and discovered vendors

Revision ID: a1b2c3d4e5f6
Revises: 2026_04_19_0001
Create Date: 2026-04-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = None  # adjust to latest migration ID
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MarketplaceVendor
    op.add_column("marketplace_vendors", sa.Column("contact_email", sa.String(255), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("scraped_data", sa.JSON(), nullable=True))
    op.add_column("marketplace_vendors", sa.Column("last_scraped_at", sa.DateTime(), nullable=True))
    op.create_index("ix_marketplace_vendors_contact_email", "marketplace_vendors", ["contact_email"])

    # DiscoveredVendor
    op.add_column("discovered_vendors", sa.Column("website", sa.String(500), nullable=True))
    op.add_column("discovered_vendors", sa.Column("contact_email", sa.String(255), nullable=True))
    op.add_column("discovered_vendors", sa.Column("scraped_data", sa.JSON(), nullable=True))
    op.add_column("discovered_vendors", sa.Column("last_scraped_at", sa.DateTime(), nullable=True))
    op.create_index("ix_discovered_vendors_contact_email", "discovered_vendors", ["contact_email"])


def downgrade() -> None:
    op.drop_index("ix_discovered_vendors_contact_email", table_name="discovered_vendors")
    op.drop_column("discovered_vendors", "last_scraped_at")
    op.drop_column("discovered_vendors", "scraped_data")
    op.drop_column("discovered_vendors", "contact_email")
    op.drop_column("discovered_vendors", "website")

    op.drop_index("ix_marketplace_vendors_contact_email", table_name="marketplace_vendors")
    op.drop_column("marketplace_vendors", "last_scraped_at")
    op.drop_column("marketplace_vendors", "scraped_data")
    op.drop_column("marketplace_vendors", "contact_email")
