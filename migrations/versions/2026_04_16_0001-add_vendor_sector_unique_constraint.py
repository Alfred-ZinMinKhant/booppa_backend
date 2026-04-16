"""add unique constraint to vendor_sectors (vendor_id, sector)

Revision ID: 2026_04_16_0001
Revises: 2026_04_14_0002
Create Date: 2026-04-16
"""
from alembic import op

revision = "2026_04_16_0001"
down_revision = "2026_04_14_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove any duplicate rows before adding the constraint (keep the oldest by ctid)
    op.execute("""
        DELETE FROM vendor_sectors
        WHERE id NOT IN (
            SELECT DISTINCT ON (vendor_id, sector) id
            FROM vendor_sectors
            ORDER BY vendor_id, sector, id
        )
    """)
    op.create_unique_constraint("uq_vendor_sector", "vendor_sectors", ["vendor_id", "sector"])


def downgrade() -> None:
    op.drop_constraint("uq_vendor_sector", "vendor_sectors", type_="unique")
