"""Add audit chain events

Revision ID: 2026_02_07_1500
Revises: 2026_01_27_1230
Create Date: 2026-02-07 15:00:00.000000+07:00

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2026_02_07_1500"
down_revision = "7a1f8b1f5b22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("report_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("hash_prev", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
    )
    op.create_index(
        op.f("ix_audit_chain_events_action"),
        "audit_chain_events",
        ["action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_chain_events_created_at"),
        "audit_chain_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_chain_events_hash"),
        "audit_chain_events",
        ["hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_chain_events_report_id"),
        "audit_chain_events",
        ["report_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_chain_events_report_id"), table_name="audit_chain_events")
    op.drop_index(op.f("ix_audit_chain_events_hash"), table_name="audit_chain_events")
    op.drop_index(op.f("ix_audit_chain_events_created_at"), table_name="audit_chain_events")
    op.drop_index(op.f("ix_audit_chain_events_action"), table_name="audit_chain_events")
    op.drop_table("audit_chain_events")
