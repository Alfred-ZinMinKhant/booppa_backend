"""add retention_purge_logs

Backs the daily retention sweep (app/workers/tasks.py:purge_retention_policies_task).
`retention_policies` already existed but nothing enforced it; this table is the
per-sweep audit evidence that enforcement actually ran, which is what turns a
documented retention control into a tested one.

`retention_policies.data_category` is deliberately NOT converted to a DB enum:
rows predating the closed category set hold arbitrary strings, and the
application reports those as unenforced rather than deleting the customer's
stored configuration on our own initiative.

Revision ID: 2026_08_08_0006
Revises: 2026_08_05_0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "2026_08_08_0006"
down_revision = "2026_08_05_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "retention_purge_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organisation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organisations.id"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: deleting the policy must not erase the record
        # that data was deleted under it. The audit row outlives the rule.
        sa.Column(
            "policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("retention_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("data_category", sa.String(100), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("cutoff", sa.DateTime(), nullable=False),
        sa.Column("rows_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_retention_purge_logs_organisation_id",
        "retention_purge_logs",
        ["organisation_id"],
    )
    op.create_index(
        "ix_retention_purge_logs_created_at", "retention_purge_logs", ["created_at"]
    )


def downgrade():
    op.drop_index("ix_retention_purge_logs_created_at", table_name="retention_purge_logs")
    op.drop_index(
        "ix_retention_purge_logs_organisation_id", table_name="retention_purge_logs"
    )
    op.drop_table("retention_purge_logs")
