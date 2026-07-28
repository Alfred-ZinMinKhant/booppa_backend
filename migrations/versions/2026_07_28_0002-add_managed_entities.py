"""add managed_entities (CSP/DPO client companies as report subjects)

Revision ID: 2026_07_28_0002
Revises: 2026_07_28_0001
Create Date: 2026-07-28

2026_07_28_0001 gave a BUYER's report subject a home on `discovered_vendors`.
A CSP/DPO account has the same shape of problem and no such home: it manages
compliance for many client companies from one login, so its own `User.company` /
`User.uen` is never the subject of what it buys.

`managed_entities` is that home — customer-owned, one row per client company,
carrying the same `verified_hint` / `verified_at` provenance contract as the other
identity carriers so the single trust path in `evidence_enricher` serves it too
(`MANAGED_ENTITY_CARRIER`).

Note `uen` is indexed but NOT unique: two CSPs may legitimately manage the same
client company, and each owns its own row.

New table only — no data migration, nothing existing changes shape.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "2026_07_28_0002"
down_revision = "2026_07_28_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_entities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("uen", sa.String(50), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("industry", sa.String(120), nullable=True),
        sa.Column("verified_hint", sa.String(255), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_managed_entities_owner_user_id", "managed_entities", ["owner_user_id"])
    op.create_index("ix_managed_entities_uen", "managed_entities", ["uen"])
    op.create_index(
        "ix_managed_entities_owner_status", "managed_entities", ["owner_user_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_managed_entities_owner_status", table_name="managed_entities")
    op.drop_index("ix_managed_entities_uen", table_name="managed_entities")
    op.drop_index("ix_managed_entities_owner_user_id", table_name="managed_entities")
    op.drop_table("managed_entities")
