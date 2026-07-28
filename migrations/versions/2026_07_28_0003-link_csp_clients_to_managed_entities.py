"""link csp_clients to managed_entities

`csp_clients` is an AML/CDD case file: it carries `legal_name` and a free-text
`uen_or_reg_no` that no identity resolver has ever read. That made it a second,
unverified store of "which company is this about" sitting beside the verified one
(`managed_entities`, 2026_07_28_0002) — and the bulk CSV importer could put an
unchecked UEN into it.

This column makes the AML file *reference* the identity store rather than rival
it. `csp_clients` keeps its own AML fields and lifecycle; identity is resolved
only through the linked `ManagedEntity` and its `verified_hint` / `verified_at`
provenance.

Nullable on purpose: existing rows are linked by a separate backfill
(`scripts/backfill_csp_client_entities.py`) rather than inside the migration, so
that ACRA lookups — network calls, one per distinct company — never run inside a
schema transaction. Backfilled rows land UNVERIFIED; an unchecked UEN must not
inherit trust it never earned.

Revision ID: 2026_07_28_0003
Revises: 2026_07_28_0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "2026_07_28_0003"
down_revision = "2026_07_28_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "csp_clients",
        sa.Column("managed_entity_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_csp_clients_managed_entity", "csp_clients", ["managed_entity_id"]
    )
    op.create_foreign_key(
        "fk_csp_clients_managed_entity",
        "csp_clients",
        "managed_entities",
        ["managed_entity_id"],
        ["id"],
        # The AML case file outlives the identity row: a CSP may archive a client
        # entity while regulation still requires the CDD record be retained.
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_csp_clients_managed_entity", "csp_clients", type_="foreignkey")
    op.drop_index("ix_csp_clients_managed_entity", table_name="csp_clients")
    op.drop_column("csp_clients", "managed_entity_id")
