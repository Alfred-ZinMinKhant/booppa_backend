"""Align feature_flags with the FeatureFlag model.

`feature_flags` was created once (`2026_03_15_0001-v10_marketplace.py`) with
`auto_activate_threshold` / `activated_at`. The model
(`app/core/models.py:FeatureFlag`) was later reshaped to
`phase` / `activation_thresholds` / `enabled_by` / `enabled_at`, and **no
migration was ever written for that change** — so every deployed database (and
the clean local reference DB) still has the original columns.

Effect in production: every flag lookup in `app/services/feature_flags.py`
raised `UndefinedColumn: column feature_flags.phase does not exist`, was caught,
and silently fell through to the default — logging a WARNING per flag per
request on `/api/v1/features/flags` and `/features/metrics`.

`phase` is NOT NULL in the model, so it needs a server_default to backfill
existing rows; the default is left in place because the model supplies its own
Python-side default for new rows and dropping it buys nothing.

Nothing reads the two removed columns (grepped across `app/`, `cms_admin/`,
`scripts/`), so they are dropped rather than left as dead weight.

Every step is existence-guarded: `entrypoint.sh` can run `alembic upgrade head`
before uvicorn boots, and a hard failure there takes the API down.

Revision ID: 2026_07_26_0003
Revises: 2026_07_26_0002
"""
import sqlalchemy as sa
from alembic import op

revision = "2026_07_26_0003"
down_revision = "2026_07_26_0002"
branch_labels = None
depends_on = None

TABLE = "feature_flags"


def _columns(bind) -> set:
    insp = sa.inspect(bind)
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if not cols:
        # Table absent entirely — the v10 migration will create it and a later
        # deploy of this revision is a no-op. Never abort the boot over it.
        return

    if "phase" not in cols:
        op.add_column(
            TABLE,
            sa.Column("phase", sa.Integer(), nullable=False, server_default="1"),
        )
    if "activation_thresholds" not in cols:
        op.add_column(TABLE, sa.Column("activation_thresholds", sa.JSON(), nullable=True))
    if "enabled_by" not in cols:
        op.add_column(TABLE, sa.Column("enabled_by", sa.String(length=100), nullable=True))
    if "enabled_at" not in cols:
        op.add_column(TABLE, sa.Column("enabled_at", sa.DateTime(), nullable=True))

    # Carry the old activation timestamp over before dropping it, so the one
    # piece of real data in the removed columns is not lost.
    if "activated_at" in cols and "enabled_at" not in cols:
        op.execute(f"UPDATE {TABLE} SET enabled_at = activated_at WHERE activated_at IS NOT NULL")

    if "activated_at" in cols:
        op.drop_column(TABLE, "activated_at")
    if "auto_activate_threshold" in cols:
        op.drop_column(TABLE, "auto_activate_threshold")

    # The model declares `enabled` NOT NULL; the original create left it
    # nullable with a false default.
    if "enabled" in cols:
        op.execute(f"UPDATE {TABLE} SET enabled = false WHERE enabled IS NULL")
        op.alter_column(TABLE, "enabled", nullable=False, server_default="false")


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if not cols:
        return

    if "auto_activate_threshold" not in cols:
        op.add_column(TABLE, sa.Column("auto_activate_threshold", sa.Integer(), nullable=True))
    if "activated_at" not in cols:
        op.add_column(TABLE, sa.Column("activated_at", sa.DateTime(), nullable=True))
        if "enabled_at" in cols:
            op.execute(f"UPDATE {TABLE} SET activated_at = enabled_at WHERE enabled_at IS NOT NULL")

    for name in ("enabled_at", "enabled_by", "activation_thresholds", "phase"):
        if name in cols:
            op.drop_column(TABLE, name)

    if "enabled" in cols:
        op.alter_column(TABLE, "enabled", nullable=True, server_default="false")
