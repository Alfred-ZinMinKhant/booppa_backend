"""add trm_document_packs

Persistence for the MAS TRM Suite document pack (Technology Risk Governance
Policy, Cyber Hygiene Attestation, Outsourcing Risk Register, Incident Response
Plan, BCM/DR Plan, IT Audit & VAPT Log, Access Control Policy). One row per
generation run.

`mas_licence_type` and `outsourcing_regime` are snapshots, not live reads: the
register inside a pack was branched on the value held at generation time, and
the organisation's value can legitimately change afterwards (a mis-selection
being corrected). Keeping the snapshot is what makes a previously delivered
register auditable.

`master_tx_hash` is String(80) to match every other on-chain column in the
schema — the `demo-0x…` sentinel is 71 characters.

Revision ID: 2026_08_03_0002
Revises: 2026_08_03_0001
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2026_08_03_0002"
down_revision = "2026_08_03_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trm_document_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organisation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_label", sa.String(length=60), nullable=True),
        sa.Column("mas_licence_type", sa.String(length=40), nullable=True),
        sa.Column("outsourcing_regime", sa.String(length=40), nullable=True),
        sa.Column(
            "schema_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="queued"
        ),
        sa.Column("documents", postgresql.JSONB(), nullable=True),
        sa.Column("master_hash", sa.String(length=64), nullable=True),
        sa.Column("master_tx_hash", sa.String(length=80), nullable=True),
        sa.Column("total_cost_usd", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
    )
    op.create_index(
        "ix_trm_document_packs_organisation_id",
        "trm_document_packs",
        ["organisation_id"],
    )
    op.create_index(
        "ix_trm_document_packs_owner_user_id",
        "trm_document_packs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_trm_document_packs_org_status",
        "trm_document_packs",
        ["organisation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_trm_document_packs_org_status", table_name="trm_document_packs")
    op.drop_index("ix_trm_document_packs_owner_user_id", table_name="trm_document_packs")
    op.drop_index(
        "ix_trm_document_packs_organisation_id", table_name="trm_document_packs"
    )
    op.drop_table("trm_document_packs")
