"""
Alembic migration — V8 Limitations Fix
======================================
Creates two new tables to replace gaps in the V8 integration:
  - anomaly_events      (replaces GovernanceRecord proxy for risk signals)
  - evidence_packages   (fixes hardcoded ep_count=0 in elevation depth)

Chains from: 2026_03_04_0001-v8_integration
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid

# revision identifiers
revision = "2026_03_04_0002"
down_revision = "2026_03_04_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── anomaly_events ──────────────────────────────────────────────────────
    op.create_table(
        "anomaly_events",
        sa.Column("id",             UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("vendor_id",      UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status",         sa.String(20),  nullable=False, server_default="OPEN"),
        sa.Column("severity",       sa.String(20),  nullable=False, server_default="LOW"),
        sa.Column("anomaly_type",   sa.String(100), nullable=False),
        sa.Column("description",    sa.Text,        nullable=True),
        sa.Column("metadata",       sa.JSON,        nullable=True),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        sa.Column("resolved_at",    sa.DateTime,    nullable=True),
        sa.Column("resolved_by",    sa.String(255), nullable=True),
        sa.Column("detected_at",    sa.DateTime,    nullable=False, server_default=sa.func.now()),
        sa.Column("created_at",     sa.DateTime,    server_default=sa.func.now()),
        sa.Column("updated_at",     sa.DateTime,    server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_anomaly_events_vendor_id",       "anomaly_events", ["vendor_id"])
    op.create_index("ix_anomaly_events_status",          "anomaly_events", ["status"])
    op.create_index("ix_anomaly_events_severity",        "anomaly_events", ["severity"])
    op.create_index("ix_anomaly_events_anomaly_type",    "anomaly_events", ["anomaly_type"])
    op.create_index("ix_anomaly_events_correlation_id",  "anomaly_events", ["correlation_id"])
    op.create_index("ix_anomaly_events_detected_at",     "anomaly_events", ["detected_at"])
    op.create_index("ix_anomaly_events_vendor_status",   "anomaly_events", ["vendor_id", "status"])
    op.create_index("ix_anomaly_events_vendor_severity", "anomaly_events", ["vendor_id", "severity"])
    op.create_index("ix_anomaly_events_status_severity", "anomaly_events", ["status", "severity"])

    # ── evidence_packages ────────────────────────────────────────────────────
    op.create_table(
        "evidence_packages",
        sa.Column("id",             UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("vendor_id",      UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status",         sa.String(20),  nullable=False, server_default="DRAFT"),
        sa.Column("sector",         sa.String(100), nullable=True),
        sa.Column("title",          sa.String(255), nullable=False),
        sa.Column("description",    sa.Text,        nullable=True),
        sa.Column("proof_ids",      sa.JSON,        nullable=False, server_default="[]"),
        sa.Column("document_count", sa.Integer,     nullable=False, server_default="0"),
        sa.Column("reviewer_notes", sa.Text,        nullable=True),
        sa.Column("reviewed_by",    sa.String(255), nullable=True),
        sa.Column("reviewed_at",    sa.DateTime,    nullable=True),
        sa.Column("submitted_at",   sa.DateTime,    nullable=True),
        sa.Column("created_at",     sa.DateTime,    server_default=sa.func.now()),
        sa.Column("updated_at",     sa.DateTime,    server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_evidence_packages_vendor_id",     "evidence_packages", ["vendor_id"])
    op.create_index("ix_evidence_packages_status",        "evidence_packages", ["status"])
    op.create_index("ix_evidence_packages_sector",        "evidence_packages", ["sector"])
    op.create_index("ix_evidence_packages_vendor_status", "evidence_packages", ["vendor_id", "status"])
    op.create_index("ix_evidence_packages_vendor_sector", "evidence_packages", ["vendor_id", "sector"])


def downgrade() -> None:
    op.drop_table("evidence_packages")
    op.drop_table("anomaly_events")
