"""add organisations.max_seats for team-collaboration seat enforcement

Revision ID: 2026_06_01_0002
Revises: 2026_06_01_0001
Create Date: 2026-06-01

Marketing promises seat limits per buyer-ladder tier:
  buyer_starter: 1 seat
  buyer_pro:     3 seats
  buyer_enterprise: unlimited

Backend had no `max_seats` column on Organisation. This adds it as nullable
INTEGER (NULL = unlimited). New orgs get the value from PLAN_TO_MAX_SEATS at
activation; existing rows backfill to NULL (treated as unlimited — we don't
retroactively shrink a customer's seat cap).
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_06_01_0002"
down_revision = "2026_06_01_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("max_seats", sa.Integer(), nullable=True),
    )

    # Backfill: existing orgs get their owner's current-plan seat cap.
    # NULL stays NULL (= unlimited) for plans we don't recognise — safer than
    # accidentally shrinking a paying customer's cap mid-deploy.
    op.execute(
        """
        UPDATE organisations o
           SET max_seats = CASE u.plan
               WHEN 'buyer_starter'           THEN 1
               WHEN 'buyer_starter_monthly'   THEN 1
               WHEN 'buyer_starter_annual'    THEN 1
               WHEN 'buyer_pro'               THEN 3
               WHEN 'buyer_pro_monthly'       THEN 3
               WHEN 'buyer_pro_annual'        THEN 3
               -- All other plans (Buyer Enterprise, Suites, legacy Enterprise,
               -- free) leave max_seats NULL (= unlimited).
               ELSE NULL
           END
          FROM users u
         WHERE u.id = o.owner_user_id;
        """
    )


def downgrade() -> None:
    op.drop_column("organisations", "max_seats")
