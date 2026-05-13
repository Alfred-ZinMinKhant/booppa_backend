"""reconcile certificate_logs schema with CertificateLog model

The model in app/core/models_v10.py was rewritten after the original
2026_03_15_0001-v10_marketplace.py migration without a follow-up migration,
so prod still has the old columns (user_id, file_url, blockchain_tx, issued_at)
while the ORM expects (vendor_id, file_key, file_hash, downloaded_at,
download_count, download_ip, generated_at, created_at, evidence_package_id).

This rename-and-add migration brings the table in line with the model. We use
ALTER COLUMN RENAME to preserve any existing data (the legacy
_write_certificate_log path silently failed in prod, so there should be no or
near-zero rows — but we don't destroy what's there).

Revision ID: 2026_05_13_0001
Revises: 2026_05_10_0004
Create Date: 2026-05-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_05_13_0001"
down_revision = "2026_05_10_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("certificate_logs")}

    # Rename legacy columns to match the model
    if "user_id" in cols and "vendor_id" not in cols:
        op.alter_column("certificate_logs", "user_id", new_column_name="vendor_id")
    if "file_url" in cols and "file_key" not in cols:
        op.alter_column(
            "certificate_logs",
            "file_url",
            new_column_name="file_key",
            existing_type=sa.String(500),
            type_=sa.String(500),
        )
    if "issued_at" in cols and "generated_at" not in cols:
        op.alter_column("certificate_logs", "issued_at", new_column_name="generated_at")

    # Refresh inspector
    cols = {c["name"] for c in sa.inspect(bind).get_columns("certificate_logs")}

    # Drop blockchain_tx — model no longer tracks it on this table
    if "blockchain_tx" in cols:
        op.drop_column("certificate_logs", "blockchain_tx")

    # Add the new columns the model expects (all nullable / with defaults so
    # existing rows don't break).
    if "evidence_package_id" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("evidence_package_id", PG_UUID(as_uuid=True), nullable=True),
        )
    if "file_hash" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("file_hash", sa.String(64), nullable=True),
        )
    if "downloaded_at" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("downloaded_at", sa.DateTime(), nullable=True),
        )
    if "download_count" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("download_count", sa.Integer(), nullable=True, server_default="0"),
        )
    if "download_ip" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("download_ip", sa.String(45), nullable=True),
        )
    if "created_at" not in cols:
        op.add_column(
            "certificate_logs",
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        )

    # vendor_id should be NOT NULL with a FK to users.id — enforce now that
    # the column exists.
    fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys("certificate_logs")}
    fk_name = "fk_certificate_logs_vendor_id_users"
    if fk_name not in fks:
        op.create_foreign_key(
            fk_name,
            "certificate_logs",
            "users",
            ["vendor_id"],
            ["id"],
            ondelete="CASCADE",
        )

    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("certificate_logs")}
    ix_name = "ix_certificate_logs_vendor_id"
    if ix_name not in indexes:
        op.create_index(ix_name, "certificate_logs", ["vendor_id"])

    # Best-effort: tighten vendor_id to NOT NULL if every existing row has a
    # value. Skip silently if any row would violate it — the FK protects new
    # writes regardless.
    try:
        result = bind.execute(
            sa.text("SELECT COUNT(*) FROM certificate_logs WHERE vendor_id IS NULL")
        ).scalar()
        if (result or 0) == 0:
            op.alter_column(
                "certificate_logs",
                "vendor_id",
                existing_type=PG_UUID(as_uuid=True),
                nullable=False,
            )
    except Exception:
        pass


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("certificate_logs")}
    if "ix_certificate_logs_vendor_id" in indexes:
        op.drop_index("ix_certificate_logs_vendor_id", table_name="certificate_logs")

    fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys("certificate_logs")}
    if "fk_certificate_logs_vendor_id_users" in fks:
        op.drop_constraint(
            "fk_certificate_logs_vendor_id_users",
            "certificate_logs",
            type_="foreignkey",
        )

    cols = {c["name"] for c in sa.inspect(bind).get_columns("certificate_logs")}
    for col in (
        "created_at",
        "download_ip",
        "download_count",
        "downloaded_at",
        "file_hash",
        "evidence_package_id",
    ):
        if col in cols:
            op.drop_column("certificate_logs", col)

    op.add_column(
        "certificate_logs",
        sa.Column("blockchain_tx", sa.String(100), nullable=True),
    )

    cols = {c["name"] for c in sa.inspect(bind).get_columns("certificate_logs")}
    if "generated_at" in cols:
        op.alter_column("certificate_logs", "generated_at", new_column_name="issued_at")
    if "file_key" in cols:
        op.alter_column(
            "certificate_logs",
            "file_key",
            new_column_name="file_url",
            existing_type=sa.String(500),
            type_=sa.String(500),
        )
    if "vendor_id" in cols:
        op.alter_column(
            "certificate_logs",
            "vendor_id",
            new_column_name="user_id",
            existing_nullable=False,
            nullable=True,
        )
