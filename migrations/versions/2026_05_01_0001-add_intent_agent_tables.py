"""add intent agent tables

Revision ID: 2026_05_01_0001
Revises: 2026_04_25_0002
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2026_05_01_0001"
down_revision = "2026_04_25_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. raw_events — stores web visit events by IP
    op.create_table(
        "raw_events",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("url_path", sa.Text(), nullable=False),
        sa.Column("category", sa.String(100)),
        sa.Column("score", sa.Float(), server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_raw_events_created_at", "raw_events", ["created_at"])
    op.create_index("idx_raw_events_ip", "raw_events", ["ip"])

    # 2. sessions — maps IPs to resolved company domains
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("detected_domain", sa.String(255)),
        sa.Column("confidence", sa.Float()),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_sessions_ip", "sessions", ["ip"])
    op.create_index("idx_sessions_detected_domain", "sessions", ["detected_domain"])

    # 3. accounts — enriched company profiles keyed by domain
    op.create_table(
        "accounts",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255)),
        sa.Column("enrichment_data", postgresql.JSONB()),
        sa.Column("last_enriched", sa.DateTime(timezone=True)),
    )

    # 4. enrichment_queue — tracks IP → domain resolution work items
    op.create_table(
        "enrichment_queue",
        sa.Column("ip", postgresql.INET(), primary_key=True),
        sa.Column("status", sa.String(20), server_default="'pending'", nullable=False),
        sa.Column("resolved_domain", sa.String(255)),
        sa.Column("confidence", sa.Float()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt", sa.DateTime(timezone=True)),
        sa.Column("fail_count", sa.Integer(), server_default="0"),
        sa.Column("last_error", sa.Text()),
    )

    # 5. icp_rules — ideal customer profile fit-factor rules
    op.create_table(
        "icp_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("industry", sa.String(100)),
        sa.Column("fit_factor", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("min_employee_range", sa.Integer()),
        sa.Column("max_employee_range", sa.Integer()),
    )

    # 6. vendor_match — maps intent categories to recommended vendors
    op.create_table(
        "vendor_match",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("vendor_name", sa.String(255), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=False),
    )

    # 7. hot_leads — scored and enriched lead pipeline (upserted by agent)
    op.create_table(
        "hot_leads",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("score", sa.Float()),
        sa.Column("summary", sa.Text()),
        sa.Column("reasons", postgresql.JSONB()),
        sa.Column("sessions_count", sa.Integer()),
        sa.Column("last_event", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("ai_insight", postgresql.JSONB()),
        sa.Column("last_score", sa.Float()),
        sa.Column("score_delta", sa.Float()),
        sa.Column("recommended_vendor", sa.String(255)),
        sa.Column("top_vendors", postgresql.JSONB()),
        sa.Column("explanation", postgresql.JSONB()),
        sa.Column("fit_score", sa.Float()),
    )

    # 8. Materialized views — require unique indexes for CONCURRENT refresh
    op.execute("""
        CREATE MATERIALIZED VIEW mv_raw_scores AS
        SELECT
            s.detected_domain,
            COALESCE(SUM(re.score), 0) AS raw_score,
            COUNT(DISTINCT s.session_id)::int AS sessions,
            MAX(re.created_at) AS last_event
        FROM sessions s
        LEFT JOIN raw_events re ON re.ip = s.ip
        WHERE s.detected_domain IS NOT NULL
        GROUP BY s.detected_domain
        WITH NO DATA;
    """)
    op.execute("CREATE UNIQUE INDEX ON mv_raw_scores (detected_domain)")

    op.execute("""
        CREATE MATERIALIZED VIEW mv_category_intent AS
        SELECT
            s.detected_domain,
            re.category,
            COUNT(re.id)::int AS views
        FROM raw_events re
        JOIN sessions s ON s.ip = re.ip
        WHERE s.detected_domain IS NOT NULL
          AND re.category IS NOT NULL
        GROUP BY s.detected_domain, re.category
        WITH NO DATA;
    """)
    op.execute("CREATE UNIQUE INDEX ON mv_category_intent (detected_domain, category)")

    # Seed initial ICP rules
    op.execute("""
        INSERT INTO icp_rules (industry, fit_factor, min_employee_range, max_employee_range) VALUES
        ('Cybersecurity', 1.5, 50, 10000),
        ('Fintech', 1.3, 20, 5000),
        ('SaaS', 1.2, 10, 2000),
        ('LegalTech', 1.4, 5, 1000);
    """)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_category_intent")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_raw_scores")
    op.drop_table("hot_leads")
    op.drop_table("vendor_match")
    op.drop_table("icp_rules")
    op.drop_table("enrichment_queue")
    op.drop_table("accounts")
    op.drop_table("sessions")
    op.drop_table("raw_events")
