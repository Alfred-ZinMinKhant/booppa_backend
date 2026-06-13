"""add vendor_evaluation_frameworks + scan-ledger on-chain columns

Two buyer-tier features:
  • Vendor evaluation frameworks (weight profiles) — powers Buyer Professional
    "customisable risk-scoring weights" and Buyer Enterprise "custom evaluation
    frameworks". New `vendor_evaluation_frameworks` table + `organisations
    .active_framework_id` pointer. Built-in templates (DEFAULT/MAS_TRM/MOH) are
    seeded lazily per-org by the frameworks API, not here.
  • On-chain per-scan verification log (Buyer Enterprise) — adds tx_hash /
    anchored_at / anchor_error to `vendor_scan_ledger`, populated async by
    anchor_scan_ledger_task.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "2026_06_13_0001"
down_revision = "2026_06_07_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── vendor_evaluation_frameworks ──────────────────────────────────────
    op.create_table(
        "vendor_evaluation_frameworks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organisation_id", UUID(as_uuid=True),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("framework_type", sa.String(32), nullable=False, server_default="CUSTOM"),
        sa.Column("sector", sa.String(120), nullable=True),
        sa.Column("weight_compliance", sa.Float(), nullable=False, server_default="0.30"),
        sa.Column("weight_visibility", sa.Float(), nullable=False, server_default="0.20"),
        sa.Column("weight_engagement", sa.Float(), nullable=False, server_default="0.20"),
        sa.Column("weight_recency", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("weight_procurement_interest", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("criteria", JSONB(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organisation_id", "name", name="uq_eval_framework_org_name"),
    )
    op.create_index(
        "ix_vendor_evaluation_frameworks_organisation_id",
        "vendor_evaluation_frameworks", ["organisation_id"],
    )
    op.create_index(
        "ix_vendor_evaluation_frameworks_sector",
        "vendor_evaluation_frameworks", ["sector"],
    )
    op.create_index(
        "ix_eval_framework_org_sector",
        "vendor_evaluation_frameworks", ["organisation_id", "sector"],
    )
    op.create_index(
        "ix_eval_framework_org_active",
        "vendor_evaluation_frameworks", ["organisation_id", "framework_type"],
    )

    # ── organisations.active_framework_id ─────────────────────────────────
    op.add_column(
        "organisations",
        sa.Column("active_framework_id", UUID(as_uuid=True), nullable=True),
    )

    # ── vendor_scan_ledger on-chain columns ───────────────────────────────
    op.add_column("vendor_scan_ledger", sa.Column("tx_hash", sa.String(128), nullable=True))
    op.add_column("vendor_scan_ledger", sa.Column("anchored_at", sa.DateTime(), nullable=True))
    op.add_column("vendor_scan_ledger", sa.Column("anchor_error", sa.Text(), nullable=True))
    op.create_index("ix_vendor_scan_ledger_tx_hash", "vendor_scan_ledger", ["tx_hash"])


def downgrade() -> None:
    op.drop_index("ix_vendor_scan_ledger_tx_hash", table_name="vendor_scan_ledger")
    op.drop_column("vendor_scan_ledger", "anchor_error")
    op.drop_column("vendor_scan_ledger", "anchored_at")
    op.drop_column("vendor_scan_ledger", "tx_hash")
    op.drop_column("organisations", "active_framework_id")
    op.drop_index("ix_eval_framework_org_active", table_name="vendor_evaluation_frameworks")
    op.drop_index("ix_eval_framework_org_sector", table_name="vendor_evaluation_frameworks")
    op.drop_index("ix_vendor_evaluation_frameworks_sector", table_name="vendor_evaluation_frameworks")
    op.drop_index("ix_vendor_evaluation_frameworks_organisation_id", table_name="vendor_evaluation_frameworks")
    op.drop_table("vendor_evaluation_frameworks")
