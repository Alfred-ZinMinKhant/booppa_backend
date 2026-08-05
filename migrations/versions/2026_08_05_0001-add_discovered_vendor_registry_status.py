"""add registry_status / registry_checked_at to discovered_vendors

The ACRA bulk sweep drops every non-live record in `_normalize` before it ever
reaches the upsert, so `discovered_vendors` could only ever say "this entity is
live" by omission — a struck-off company and a company we never fetched were
indistinguishable (both absent). For a UEN a customer actually looked up, the
struck-off fact is the single most valuable thing in the register.

The forthcoming targeted refresh pass keeps those records and records the raw
ACRA status here. Deliberately nullable with no backfill: NULL means "never
targeted-checked", which is NOT "live". Backfilling existing rows with a live
token would manufacture a positive registry claim for ~50k entities whose status
we last confirmed only implicitly, at an unknown date — exactly the kind of
untested assertion this codebase treats as a layer-3 failure. Readers must render
NULL as absent.

Revision ID: 2026_08_05_0001
Revises: 2026_08_04_0001
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_08_05_0001"
down_revision = "2026_08_04_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovered_vendors",
        sa.Column("registry_status", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "discovered_vendors",
        sa.Column("registry_checked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovered_vendors", "registry_checked_at")
    op.drop_column("discovered_vendors", "registry_status")
