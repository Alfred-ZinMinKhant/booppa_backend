"""add tender_check_lookups and user.tender_lookup_opt_out

Revision ID: 2026_05_23_0002
Revises: 2026_05_23_0001
Create Date: 2026-05-23

Powers the Vendor Pro competitor-awareness signal: every /tender-check
call writes one row here (subject to user opt-out). The endpoint then
queries this table to surface anonymised lookup counts.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "2026_05_23_0002"
down_revision = "2026_05_23_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tender_check_lookups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tender_no", sa.String(length=100), nullable=True),
        sa.Column("vendor_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column(
            "is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_tender_check_lookups_tender_no", "tender_check_lookups", ["tender_no"])
    op.create_index("ix_tender_check_lookups_vendor_id", "tender_check_lookups", ["vendor_id"])
    op.create_index("ix_tender_check_lookups_sector", "tender_check_lookups", ["sector"])
    op.create_index("ix_tender_check_lookups_is_verified", "tender_check_lookups", ["is_verified"])
    op.create_index("ix_tender_check_lookups_created_at", "tender_check_lookups", ["created_at"])
    op.create_index(
        "ix_tender_check_lookups_tender_created",
        "tender_check_lookups",
        ["tender_no", "created_at"],
    )
    op.create_index(
        "ix_tender_check_lookups_sector_created",
        "tender_check_lookups",
        ["sector", "created_at"],
    )

    # Per-vendor opt-out from being logged in the lookup table.
    op.add_column(
        "users",
        sa.Column(
            "tender_lookup_opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "tender_lookup_opt_out")
    op.drop_index("ix_tender_check_lookups_sector_created", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_tender_created", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_created_at", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_is_verified", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_sector", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_vendor_id", table_name="tender_check_lookups")
    op.drop_index("ix_tender_check_lookups_tender_no", table_name="tender_check_lookups")
    op.drop_table("tender_check_lookups")
