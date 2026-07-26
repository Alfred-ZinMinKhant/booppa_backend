"""Widen tx_hash columns from varchar(66) to varchar(80).

A real Polygon tx hash is `0x` + 64 hex = 66 chars, which is what these columns
were sized for. Demo / admin test-checkout anchoring stores
`supplier_due_diligence_generator.demo_tx_hash()` instead — `demo-0x` + 64 hex =
**71 chars** — and the `demo-` prefix is load-bearing (it must fail
`tx_utils.is_real_onchain_tx` so no renderer prints a demo value as a verified
anchor). The result was `StringDataRightTruncation` on every demo-mode
fulfillment, raised *after* the PDF was generated, uploaded and anchored, so the
task retried forever and the buyer got a "small delay" email per retry.

Each ALTER is guarded on table/column existence: several `csp_*` models are
known to be drifted from the deployed schema, and `entrypoint.sh` runs
`alembic upgrade head` before uvicorn boots — an ALTER on a missing table would
abort the boot.

Revision ID: 2026_07_26_0002
Revises: 2026_07_26_0001
"""
import sqlalchemy as sa
from alembic import op

revision = "2026_07_26_0002"
down_revision = "2026_07_26_0001"
branch_labels = None
depends_on = None


# (table, column) pairs — every String(66) column in app/core/models.py.
_TX_COLUMNS = [
    ("reports", "tx_hash"),
    ("csp_blockchain_evidence", "tx_hash"),
    ("trm_evidence", "tx_hash"),
    ("csp_cdd_records", "blockchain_tx_hash"),
    ("csp_edd_records", "blockchain_tx_hash"),
    ("csp_str_reports", "blockchain_tx_hash"),
    ("csp_nominee_directors", "blockchain_tx_hash"),
    ("csp_nominee_shareholders", "blockchain_tx_hash"),
    ("csp_beneficial_owners", "blockchain_tx_hash"),
    ("csp_aml_programme", "blockchain_tx_hash"),
    ("csp_risk_assessments", "blockchain_tx_hash"),
    ("csp_staff_training", "blockchain_tx_hash"),
    ("csp_tos_acceptances", "blockchain_tx_hash"),
    ("csp_programme_attestations", "blockchain_tx_hash"),
    ("csp_risk_classification_audits", "blockchain_tx_hash"),
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table, column in _TX_COLUMNS:
        if table not in existing_tables:
            continue
        found = {c["name"]: c for c in inspector.get_columns(table)}
        if column not in found:
            continue
        op.alter_column(
            table,
            column,
            existing_type=sa.String(66),
            type_=sa.String(80),
            existing_nullable=found[column]["nullable"],
        )


def downgrade() -> None:
    # Intentionally a no-op: narrowing back to varchar(66) would truncate (and
    # therefore corrupt) any demo tx hash already stored at full length.
    pass
