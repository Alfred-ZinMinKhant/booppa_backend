"""add gebiz_award_history table

Revision ID: 2026_05_23_0001
Revises: 2026_05_19_0001
Create Date: 2026-05-23

Persists row-level GeBIZ award rows from data.gov.sg so the Tender
Intelligence product can serve historical award lookups and trend
analytics. The existing refresh_gebiz_base_rates task continues to
maintain TenderShortlist.base_rate; this table holds the raw rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "2026_05_23_0001"
down_revision = "2026_05_19_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gebiz_award_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tender_no", sa.String(length=100), nullable=True),
        sa.Column("awarded_date", sa.Date(), nullable=True),
        sa.Column("supplier_name", sa.String(length=255), nullable=True),
        sa.Column("award_amt", sa.Numeric(14, 2), nullable=True),
        sa.Column("tender_description", sa.Text(), nullable=True),
        sa.Column("procuring_entity", sa.String(length=255), nullable=True),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tender_no", "supplier_name", "awarded_date",
            name="uq_gebiz_award_history_tender_supplier_date",
        ),
    )
    op.create_index(
        "ix_gebiz_award_history_tender_no",
        "gebiz_award_history",
        ["tender_no"],
    )
    op.create_index(
        "ix_gebiz_award_history_awarded_date",
        "gebiz_award_history",
        ["awarded_date"],
    )
    op.create_index(
        "ix_gebiz_award_history_supplier_name",
        "gebiz_award_history",
        ["supplier_name"],
    )
    op.create_index(
        "ix_gebiz_award_history_procuring_entity",
        "gebiz_award_history",
        ["procuring_entity"],
    )
    op.create_index(
        "ix_gebiz_award_history_sector",
        "gebiz_award_history",
        ["sector"],
    )
    op.create_index(
        "ix_gebiz_award_entity_date",
        "gebiz_award_history",
        ["procuring_entity", "awarded_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_gebiz_award_entity_date", table_name="gebiz_award_history")
    op.drop_index("ix_gebiz_award_history_sector", table_name="gebiz_award_history")
    op.drop_index("ix_gebiz_award_history_procuring_entity", table_name="gebiz_award_history")
    op.drop_index("ix_gebiz_award_history_supplier_name", table_name="gebiz_award_history")
    op.drop_index("ix_gebiz_award_history_awarded_date", table_name="gebiz_award_history")
    op.drop_index("ix_gebiz_award_history_tender_no", table_name="gebiz_award_history")
    op.drop_table("gebiz_award_history")
