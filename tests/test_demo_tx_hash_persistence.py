"""Guard: every tx-hash column must be wide enough for a demo tx hash.

A real Polygon tx is 66 chars, but demo / admin test-checkout anchoring stores
`demo_tx_hash()` — `demo-0x` + 64 hex = 71 chars. When the columns were
String(66) every demo-mode fulfillment died on StringDataRightTruncation after
the PDF had already been generated, uploaded and anchored, then retried forever.

These are static schema assertions, so they run without a database.
"""
import pytest
from sqlalchemy import String

from app.core.models import Base
from app.services.supplier_due_diligence_generator import demo_tx_hash

_DEMO_LEN = len(demo_tx_hash("any-evidence-hash"))


def _tx_columns():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.name in ("tx_hash", "blockchain_tx_hash") and isinstance(
                col.type, String
            ):
                yield table.name, col


def test_demo_tx_hash_is_longer_than_a_real_tx():
    # If this ever stops being true the demo value would be shape-valid and could
    # be rendered as a genuine anchor — see tx_utils.is_real_onchain_tx.
    assert _DEMO_LEN > 66
    assert demo_tx_hash("x").startswith("demo-0x")


@pytest.mark.parametrize("table,column", [(t, c) for t, c in _tx_columns()],
                         ids=lambda v: v if isinstance(v, str) else v.name)
def test_tx_columns_fit_a_demo_tx_hash(table, column):
    length = column.type.length
    assert length is not None, f"{table}.{column.name} has unbounded length"
    assert length >= _DEMO_LEN, (
        f"{table}.{column.name} is String({length}) but a demo tx hash is "
        f"{_DEMO_LEN} chars — writes will raise StringDataRightTruncation"
    )
