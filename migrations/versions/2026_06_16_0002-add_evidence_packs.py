"""add evidence_packs table (BCEP compliance evidence pack)

The compliance_evidence_pack SKU now produces the BCEP 7-document governance pack
instead of the cover-sheet-only flow. One EvidencePack row per purchase tracks the
structured intake, the generated documents, their hashes + anchoring, and the S3
download URLs through the pipeline (intake_pending → queued → generating →
anchoring → building_pdfs → ready | error).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "2026_06_16_0002"
down_revision = "2026_06_16_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_packs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("pack_id", sa.String(120), nullable=False, unique=True),  # → evidence_packs_pack_id_key
        sa.Column(
            "user_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("organisation", sa.String(255), nullable=True),
        sa.Column("intake", JSONB, nullable=True),
        sa.Column("documents", JSONB, nullable=True),
        sa.Column("hashes", JSONB, nullable=True),
        sa.Column("master_hash", sa.String(64), nullable=True),
        sa.Column("anchoring", JSONB, nullable=True),
        sa.Column("download_urls", JSONB, nullable=True),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    # pack_id uniqueness comes from the column-level unique constraint above
    # (evidence_packs_pack_id_key); no separate unique index needed.
    op.create_index("ix_evidence_packs_user_id", "evidence_packs", ["user_id"])
    op.create_index("ix_evidence_packs_session_id", "evidence_packs", ["session_id"])
    op.create_index("ix_evidence_packs_user_status", "evidence_packs", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_evidence_packs_user_status", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_session_id", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_user_id", table_name="evidence_packs")
    op.drop_table("evidence_packs")
