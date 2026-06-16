"""add uen column to pending_rfp_intakes

Audit fix: UEN (Singapore Business Registration No.) is now mandatory before an
RFP Complete Kit is generated — it is the field GeBIZ procurement officers check
first, and kits previously shipped with UEN "Not provided". The intake submit
endpoint collects + persists it here. The "needs_more_info" status (set when a kit
is blocked at the placeholder gate and the buyer is routed back to complete it)
needs no schema change — it reuses the existing `status` column.
"""

from alembic import op
import sqlalchemy as sa


revision = "2026_06_16_0001"
down_revision = "2026_06_13_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_rfp_intakes",
        sa.Column("uen", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pending_rfp_intakes", "uen")
