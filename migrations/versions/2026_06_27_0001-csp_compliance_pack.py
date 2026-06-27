"""CSP Compliance Pack — organisations, profiles, AML/CFT records + v3 legal layer

Revision ID: 2026_06_27_0001
Revises: 2026_06_26_0002
Create Date: 2026-06-27

Ports the booppa-csp-pack-v3 schema (pack migrations 003 + 004) onto Booppa's Alembic
chain. Adds the CSP organisation/tenancy tables, the full AML/CFT record set, and the v3
legal-protection tables with DB-level CHECK constraints (no record can exist with a
declaration/checkbox left false, even if Pydantic is bypassed).

PII columns stay VARCHAR/TEXT — EncryptedString/EncryptedText is a transparent
application-layer TypeDecorator, so no special column type is needed.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "2026_06_27_0001"
down_revision = "2026_06_26_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── csp_organisations (tenancy) ─────────────────────────────────────
    op.create_table("csp_organisations",
        sa.Column("id",            postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name",          sa.String(255), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan",          sa.String(50), server_default="'full'"),
        sa.Column("monthly_fee_sgd", sa.Float(), server_default="299.0"),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_org_owner", "csp_organisations", ["owner_user_id"])

    op.create_table("csp_org_memberships",
        sa.Column("id",      postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id",  postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role",    sa.String(50), server_default="'csp_admin'"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("org_id", "user_id", name="uq_csp_org_member"),
    )
    op.create_index("ix_csp_org_mem_org",  "csp_org_memberships", ["org_id"])
    op.create_index("ix_csp_org_mem_user", "csp_org_memberships", ["user_id"])

    # ── csp_profiles ────────────────────────────────────────────────────
    op.create_table("csp_profiles",
        sa.Column("id",               postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id",  postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_organisations.id"), nullable=False, unique=True),
        sa.Column("legal_name",       sa.String(255), nullable=False),
        sa.Column("uen",              sa.String(20), nullable=False, unique=True),
        sa.Column("registered_address", sa.Text()),
        sa.Column("business_email",   sa.String(255)),
        sa.Column("business_phone",   sa.String(50)),
        sa.Column("acra_reg_status",  sa.String(30), server_default="'not_started'"),
        sa.Column("acra_reg_number",  sa.String(50)),
        sa.Column("acra_reg_date",    sa.DateTime(timezone=True)),
        sa.Column("acra_renewal_date",sa.DateTime(timezone=True)),
        sa.Column("acra_licence_type",sa.String(100)),
        sa.Column("rqi_name",         sa.String(255)),
        sa.Column("rqi_qualification",sa.String(255)),
        sa.Column("rqi_training_completed", sa.Boolean(), server_default="false"),
        sa.Column("rqi_training_date",sa.DateTime(timezone=True)),
        sa.Column("rqi_acra_registration_no", sa.String(50)),
        sa.Column("offers_company_formation",   sa.Boolean(), server_default="false"),
        sa.Column("offers_nominee_director",     sa.Boolean(), server_default="false"),
        sa.Column("offers_nominee_shareholder",  sa.Boolean(), server_default="false"),
        sa.Column("offers_registered_address",   sa.Boolean(), server_default="false"),
        sa.Column("offers_corp_secretarial",     sa.Boolean(), server_default="false"),
        sa.Column("offers_shelf_company",        sa.Boolean(), server_default="false"),
        sa.Column("aml_programme_exists",    sa.Boolean(), server_default="false"),
        sa.Column("aml_programme_version",   sa.String(20)),
        sa.Column("aml_programme_reviewed",  sa.DateTime(timezone=True)),
        sa.Column("aml_compliance_officer",  sa.String(255)),
        sa.Column("overall_compliance_score",sa.Float(), server_default="0.0"),
        sa.Column("last_scored_at",          sa.DateTime(timezone=True)),
        sa.Column("csp_pack_tier",           sa.String(20), server_default="'full'"),
        sa.Column("amount_paid_sgd",         sa.Float()),
        sa.Column("monthly_fee_sgd",         sa.Float(), server_default="299.0"),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_profiles_org", "csp_profiles", ["organisation_id"])

    # ── csp_clients ─────────────────────────────────────────────────────
    op.create_table("csp_clients",
        sa.Column("id",              postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",          postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_type",     sa.String(30), nullable=False),
        sa.Column("legal_name",      sa.String(255), nullable=False),
        sa.Column("uen_or_reg_no",   sa.String(50)),
        sa.Column("country_of_inc",  sa.String(100)),
        sa.Column("registered_address", sa.Text()),
        sa.Column("contact_name",    sa.String(255)),
        sa.Column("contact_email",   sa.String(255)),
        sa.Column("contact_phone",   sa.String(50)),
        sa.Column("services_provided", postgresql.JSONB()),
        sa.Column("onboarded_at",    sa.DateTime(timezone=True)),
        sa.Column("offboarded_at",   sa.DateTime(timezone=True)),
        sa.Column("is_active",       sa.Boolean(), server_default="true"),
        sa.Column("risk_rating",     sa.String(20), server_default="'medium'"),
        sa.Column("risk_rationale",  sa.Text()),
        sa.Column("is_pep",          sa.Boolean(), server_default="false"),
        sa.Column("pep_details",     sa.Text()),
        sa.Column("high_risk_country", sa.Boolean(), server_default="false"),
        sa.Column("country_risk_basis", sa.String(255)),
        sa.Column("cdd_status",      sa.String(30), server_default="'not_started'"),
        sa.Column("cdd_completed_at",sa.DateTime(timezone=True)),
        sa.Column("cdd_next_review", sa.DateTime(timezone=True)),
        sa.Column("edd_required",    sa.Boolean(), server_default="false"),
        sa.Column("edd_trigger",     sa.String(50)),
        sa.Column("has_nominee_director",    sa.Boolean(), server_default="false"),
        sa.Column("has_nominee_shareholder", sa.Boolean(), server_default="false"),
        sa.Column("is_remote_onboarding",    sa.Boolean(), server_default="false"),
        sa.Column("video_call_completed",    sa.Boolean(), server_default="false"),
        sa.Column("video_call_date",         sa.DateTime(timezone=True)),
        sa.Column("video_call_conducted_by", sa.String(255)),
        sa.Column("str_filed",  sa.Boolean(), server_default="false"),
        sa.Column("str_count",  sa.Integer(), server_default="0"),
        sa.Column("sanctions_screened",    sa.Boolean(), server_default="false"),
        sa.Column("sanctions_clear",       sa.Boolean()),
        sa.Column("sanctions_screened_at", sa.DateTime(timezone=True)),
        sa.Column("sanctions_hits",        postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_clients_csp_status", "csp_clients", ["csp_id","cdd_status"])
    op.create_index("ix_csp_clients_risk",       "csp_clients", ["csp_id","risk_rating"])

    # ── csp_cdd_records ──────────────────────────────────────────────────
    op.create_table("csp_cdd_records",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id",   postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csp_id",      postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("review_type", sa.String(30)),
        sa.Column("individual_full_name",        sa.String(255)),
        sa.Column("individual_nric_or_passport", sa.String(300)),   # EncryptedString
        sa.Column("individual_dob",              sa.String(20)),
        sa.Column("individual_nationality",      sa.String(100)),
        sa.Column("individual_address",          sa.Text()),        # EncryptedText
        sa.Column("id_doc_type",                 sa.String(50)),
        sa.Column("id_doc_verified",             sa.Boolean(), server_default="false"),
        sa.Column("id_doc_expiry",               sa.String(20)),
        sa.Column("id_verification_method",      sa.String(50)),
        sa.Column("corp_registration_verified",  sa.Boolean(), server_default="false"),
        sa.Column("corp_constitution_obtained",  sa.Boolean(), server_default="false"),
        sa.Column("corp_directors_identified",   sa.Boolean(), server_default="false"),
        sa.Column("corp_shareholders_identified",sa.Boolean(), server_default="false"),
        sa.Column("business_purpose",            sa.Text()),
        sa.Column("source_of_funds",             sa.Text()),
        sa.Column("source_of_wealth",            sa.Text()),
        sa.Column("expected_transactions",       sa.Text()),
        sa.Column("non_face_to_face",            sa.Boolean(), server_default="false"),
        sa.Column("video_call_completed",        sa.Boolean(), server_default="false"),
        sa.Column("video_call_recording_ref",    sa.String(255)),
        sa.Column("sanctions_screened",          sa.Boolean(), server_default="false"),
        sa.Column("sanctions_clear",             sa.Boolean()),
        sa.Column("sanctions_screen_date",       sa.DateTime(timezone=True)),
        sa.Column("sanctions_screen_provider",   sa.String(100)),
        sa.Column("pep_screening_done",          sa.Boolean(), server_default="false"),
        sa.Column("pep_result",                  sa.String(50)),
        sa.Column("adverse_media_checked",       sa.Boolean(), server_default="false"),
        sa.Column("status",          sa.String(30), server_default="'in_progress'"),
        sa.Column("completed_by",    sa.String(255)),
        sa.Column("completed_at",    sa.DateTime(timezone=True)),
        sa.Column("next_review_date",sa.DateTime(timezone=True)),
        sa.Column("failure_reason",  sa.Text()),
        sa.Column("evidence_files",  postgresql.JSONB()),
        sa.Column("blockchain_tx_hash",   sa.String(66)),
        sa.Column("blockchain_timestamp", sa.DateTime(timezone=True)),
        sa.Column("polygonscan_url",      sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_cdd_client_date", "csp_cdd_records", ["client_id","completed_at"])

    # ── csp_edd_records ──────────────────────────────────────────────────
    op.create_table("csp_edd_records",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id",    postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("trigger",      sa.String(50), nullable=False),
        sa.Column("trigger_detail", sa.Text()),
        sa.Column("senior_mgmt_approval",      sa.Boolean(), server_default="false"),
        sa.Column("senior_mgmt_approver",      sa.String(255)),
        sa.Column("senior_mgmt_approval_date", sa.DateTime(timezone=True)),
        sa.Column("enhanced_source_of_funds",  sa.Boolean(), server_default="false"),
        sa.Column("enhanced_source_of_wealth", sa.Boolean(), server_default="false"),
        sa.Column("enhanced_business_purpose", sa.Boolean(), server_default="false"),
        sa.Column("enhanced_sanctions_screen", sa.Boolean(), server_default="false"),
        sa.Column("ongoing_monitoring_freq",   sa.String(50)),
        sa.Column("pep_name",          sa.String(255)),
        sa.Column("pep_position",      sa.String(255)),
        sa.Column("pep_country",       sa.String(100)),
        sa.Column("pep_relationship",  sa.String(100)),
        sa.Column("edd_conclusion",    sa.Text()),
        sa.Column("risk_accepted",     sa.Boolean()),
        sa.Column("risk_accepted_by",  sa.String(255)),
        sa.Column("conditions_imposed",sa.Text()),
        sa.Column("status",            sa.String(30), server_default="'in_progress'"),
        sa.Column("completed_at",      sa.DateTime(timezone=True)),
        sa.Column("evidence_files",    postgresql.JSONB()),
        sa.Column("blockchain_tx_hash",sa.String(66)),
        sa.Column("polygonscan_url",   sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_edd_client", "csp_edd_records", ["client_id"])

    # ── csp_str_reports ──────────────────────────────────────────────────
    op.create_table("csp_str_reports",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("client_id",    postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id"), nullable=True),
        sa.Column("trigger_type", sa.String(100)),
        sa.Column("trigger_detail", sa.Text(), nullable=False),
        sa.Column("amount_involved",sa.Float()),
        sa.Column("currency",       sa.String(10)),
        sa.Column("transaction_date", sa.DateTime(timezone=True)),
        sa.Column("decision",         sa.String(30), nullable=False),
        sa.Column("decision_by",      sa.String(255)),
        sa.Column("decision_date",    sa.DateTime(timezone=True)),
        sa.Column("decision_rationale", sa.Text(), nullable=False),
        sa.Column("stro_reference",   sa.String(100)),
        sa.Column("stro_filed_date",  sa.DateTime(timezone=True)),
        sa.Column("stro_filed_by",    sa.String(255)),
        sa.Column("client_notified",  sa.Boolean(), server_default="false"),
        sa.Column("service_declined", sa.Boolean(), server_default="false"),
        sa.Column("escalated_to_senior_mgmt", sa.Boolean(), server_default="false"),
        sa.Column("senior_mgmt_name",         sa.String(255)),
        sa.Column("escalation_date",          sa.DateTime(timezone=True)),
        sa.Column("evidence_files",     postgresql.JSONB()),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_str_date", "csp_str_reports", ["csp_id","decision_date"])

    # ── csp_nominee_directors ────────────────────────────────────────────
    op.create_table("csp_nominee_directors",
        sa.Column("id",      postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",  postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id"), nullable=False),
        sa.Column("nominee_full_name",         sa.String(255), nullable=False),
        sa.Column("nominee_nric_or_passport",  sa.String(300)),     # EncryptedString
        sa.Column("nominee_nationality",       sa.String(100)),
        sa.Column("nominee_address",           sa.Text()),          # EncryptedText
        sa.Column("nominator_name",            sa.String(255), nullable=False),
        sa.Column("nominator_id",              sa.String(300)),     # EncryptedString
        sa.Column("nominator_relationship",    sa.Text()),
        sa.Column("company_name",              sa.String(255)),
        sa.Column("company_uen",               sa.String(20)),
        sa.Column("appointment_date",          sa.DateTime(timezone=True)),
        sa.Column("cessation_date",            sa.DateTime(timezone=True)),
        sa.Column("is_active",                 sa.Boolean(), server_default="true"),
        sa.Column("assessment_status",         sa.String(30), server_default="'not_assessed'"),
        sa.Column("assessment_date",           sa.DateTime(timezone=True)),
        sa.Column("assessed_by",               sa.String(255)),
        sa.Column("criminal_check_done",       sa.Boolean(), server_default="false"),
        sa.Column("bankruptcy_check_done",     sa.Boolean(), server_default="false"),
        sa.Column("director_history_check",    sa.Boolean(), server_default="false"),
        sa.Column("assessment_outcome",        sa.Text()),
        sa.Column("assessment_notes",          sa.Text()),
        sa.Column("acra_disclosed",            sa.Boolean(), server_default="false"),
        sa.Column("acra_filing_date",          sa.DateTime(timezone=True)),
        sa.Column("acra_filing_ref",           sa.String(100)),
        sa.Column("last_reviewed",             sa.DateTime(timezone=True)),
        sa.Column("next_review",               sa.DateTime(timezone=True)),
        sa.Column("evidence_files",            postgresql.JSONB()),
        sa.Column("blockchain_tx_hash",        sa.String(66)),
        sa.Column("polygonscan_url",           sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_nominees_csp", "csp_nominee_directors", ["csp_id"])

    # ── csp_nominee_shareholders ─────────────────────────────────────────
    op.create_table("csp_nominee_shareholders",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("client_id",    postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id"), nullable=False),
        sa.Column("nominee_full_name",        sa.String(255), nullable=False),
        sa.Column("nominee_nric_or_passport", sa.String(300)),      # EncryptedString
        sa.Column("nominee_nationality",      sa.String(100)),
        sa.Column("nominator_name",           sa.String(255), nullable=False),
        sa.Column("nominator_id",             sa.String(300)),      # EncryptedString
        sa.Column("shares_held",              sa.String(100)),
        sa.Column("share_percentage",         sa.Float()),
        sa.Column("company_name",             sa.String(255)),
        sa.Column("company_uen",              sa.String(20)),
        sa.Column("appointment_date",         sa.DateTime(timezone=True)),
        sa.Column("cessation_date",           sa.DateTime(timezone=True)),
        sa.Column("is_active",                sa.Boolean(), server_default="true"),
        sa.Column("acra_disclosed",           sa.Boolean(), server_default="false"),
        sa.Column("acra_filing_date",         sa.DateTime(timezone=True)),
        sa.Column("acra_filing_ref",          sa.String(100)),
        sa.Column("evidence_files",           postgresql.JSONB()),
        sa.Column("blockchain_tx_hash",       sa.String(66)),
        sa.Column("polygonscan_url",          sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ── csp_beneficial_owners ────────────────────────────────────────────
    op.create_table("csp_beneficial_owners",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id",    postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("ubo_full_name",           sa.String(255), nullable=False),
        sa.Column("ubo_nric_or_passport",    sa.String(300)),       # EncryptedString
        sa.Column("ubo_nationality",         sa.String(100)),
        sa.Column("ubo_dob",                 sa.String(20)),
        sa.Column("ubo_address",             sa.Text()),            # EncryptedText
        sa.Column("ubo_country_of_residence",sa.String(100)),
        sa.Column("ownership_percentage",    sa.Float()),
        sa.Column("control_mechanism",       sa.String(255)),
        sa.Column("is_pep",                  sa.Boolean(), server_default="false"),
        sa.Column("is_sanctioned",           sa.Boolean(), server_default="false"),
        sa.Column("identity_verified",       sa.Boolean(), server_default="false"),
        sa.Column("verification_method",     sa.String(100)),
        sa.Column("verification_date",       sa.DateTime(timezone=True)),
        sa.Column("verified_by",             sa.String(255)),
        sa.Column("verification_doc",        sa.String(255)),
        sa.Column("last_updated",            sa.DateTime(timezone=True)),
        sa.Column("next_review",             sa.DateTime(timezone=True)),
        sa.Column("evidence_files",          postgresql.JSONB()),
        sa.Column("blockchain_tx_hash",      sa.String(66)),
        sa.Column("polygonscan_url",         sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_ubo_client", "csp_beneficial_owners", ["client_id"])

    # ── csp_aml_programme ────────────────────────────────────────────────
    op.create_table("csp_aml_programme",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",   postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("version",  sa.Integer(), server_default="1"),
        sa.Column("is_current", sa.Boolean(), server_default="true"),
        sa.Column("status",   sa.String(30), server_default="'draft'"),
        sa.Column("risk_assessment_section",    sa.Text()),
        sa.Column("cdd_procedures_section",     sa.Text()),
        sa.Column("edd_procedures_section",     sa.Text()),
        sa.Column("str_procedures_section",     sa.Text()),
        sa.Column("record_keeping_section",     sa.Text()),
        sa.Column("training_policy_section",    sa.Text()),
        sa.Column("governance_section",         sa.Text()),
        sa.Column("nominee_procedures_section", sa.Text()),
        sa.Column("approved_by",       sa.String(255)),
        sa.Column("approved_at",       sa.DateTime(timezone=True)),
        sa.Column("next_review_date",  sa.DateTime(timezone=True)),
        sa.Column("generated_by_model",sa.String(100), server_default="'deepseek-chat'"),
        sa.Column("generation_cost_usd", sa.Float()),
        sa.Column("s3_key",            sa.String(500)),
        sa.Column("pdf_hash",          sa.String(64)),
        sa.Column("blockchain_tx_hash",   sa.String(66)),
        sa.Column("blockchain_timestamp", sa.DateTime(timezone=True)),
        sa.Column("polygonscan_url",      sa.String(500)),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("csp_id","version", name="uq_aml_csp_version"),
    )

    # ── csp_risk_assessments ─────────────────────────────────────────────
    op.create_table("csp_risk_assessments",
        sa.Column("id",              postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csp_id",          postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("assessment_date", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("assessed_by",     sa.String(255)),
        sa.Column("country_risk",    sa.Integer()),
        sa.Column("industry_risk",   sa.Integer()),
        sa.Column("product_risk",    sa.Integer()),
        sa.Column("delivery_risk",   sa.Integer()),
        sa.Column("customer_risk",   sa.Integer()),
        sa.Column("transaction_risk",sa.Integer()),
        sa.Column("composite_score", sa.Float()),
        sa.Column("risk_rating",     sa.String(20)),
        sa.Column("edd_required",    sa.Boolean(), server_default="false"),
        sa.Column("review_frequency",sa.String(50)),
        sa.Column("next_review_date",sa.DateTime(timezone=True)),
        sa.Column("notes",           sa.Text()),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_risk_client", "csp_risk_assessments", ["client_id"])

    # ── csp_compliance_calendar ──────────────────────────────────────────
    op.create_table("csp_compliance_calendar",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",   postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("pillar",   sa.String(50), nullable=False),
        sa.Column("title",    sa.String(255), nullable=False),
        sa.Column("description",     sa.Text()),
        sa.Column("due_date",        sa.DateTime(timezone=True), nullable=False),
        sa.Column("frequency",       sa.String(30)),
        sa.Column("legal_basis",     sa.String(255)),
        sa.Column("penalty_if_missed", sa.String(255)),
        sa.Column("status",          sa.String(30), server_default="'pending'"),
        sa.Column("completed_at",    sa.DateTime(timezone=True)),
        sa.Column("completed_by",    sa.String(255)),
        sa.Column("evidence_ref",    sa.String(255)),
        sa.Column("alert_30_days_sent", sa.Boolean(), server_default="false"),
        sa.Column("alert_14_days_sent", sa.Boolean(), server_default="false"),
        sa.Column("alert_7_days_sent",  sa.Boolean(), server_default="false"),
        sa.Column("alert_overdue_sent", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_cal_due",    "csp_compliance_calendar", ["csp_id","due_date"])
    op.create_index("ix_csp_cal_status", "csp_compliance_calendar", ["csp_id","status"])

    # ── csp_staff_training ───────────────────────────────────────────────
    op.create_table("csp_staff_training",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("staff_name",   sa.String(255), nullable=False),
        sa.Column("staff_role",   sa.String(100)),
        sa.Column("is_rqi",       sa.Boolean(), server_default="false"),
        sa.Column("training_type",  sa.String(100)),
        sa.Column("training_title", sa.String(255)),
        sa.Column("provider",       sa.String(255)),
        sa.Column("training_date",  sa.DateTime(timezone=True)),
        sa.Column("completion_date",sa.DateTime(timezone=True)),
        sa.Column("expiry_date",    sa.DateTime(timezone=True)),
        sa.Column("status",         sa.String(30), server_default="'not_started'"),
        sa.Column("score",          sa.Integer()),
        sa.Column("certificate_ref",sa.String(255)),
        sa.Column("evidence_s3_key",sa.String(500)),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_training_staff", "csp_staff_training", ["csp_id","staff_name"])

    # ── csp_blockchain_evidence ──────────────────────────────────────────
    op.create_table("csp_blockchain_evidence",
        sa.Column("id",             postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",         postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id"), nullable=False),
        sa.Column("record_type",    sa.String(50), nullable=False),
        sa.Column("record_id",      postgresql.UUID(as_uuid=True)),
        sa.Column("record_title",   sa.String(255)),
        sa.Column("related_client", sa.String(255)),
        sa.Column("document_hash",  sa.String(64), nullable=False),
        sa.Column("tx_hash",        sa.String(66), nullable=False),
        sa.Column("block_number",   sa.Integer()),
        sa.Column("network",        sa.String(50), server_default="'polygon-amoy'"),
        sa.Column("blockchain_timestamp", sa.DateTime(timezone=True)),
        sa.Column("polygonscan_url",      sa.String(500)),
        sa.Column("gas_used",             sa.Integer()),
        sa.Column("metadata_payload",     sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_csp_evidence_type", "csp_blockchain_evidence", ["csp_id","record_type"])
    op.create_index("ix_csp_evidence_tx",   "csp_blockchain_evidence", ["tx_hash"])

    # ── v3 legal-protection layer ────────────────────────────────────────

    # csp_tos_acceptances (Intervento 3) — org-scoped, all checkboxes must be TRUE
    op.create_table("csp_tos_acceptances",
        sa.Column("id",        postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",    postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id",   postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("user_email",sa.String(255), nullable=False),
        sa.Column("tos_version", sa.String(20), nullable=False, server_default="'1.0'"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ip_address",  sa.String(45)),
        sa.Column("user_agent",  sa.String(500)),
        sa.Column("checkbox_ai_disclaimer",        sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("checkbox_data_accuracy",        sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("checkbox_sanctions_limitation", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("checkbox_regulatory_change",    sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("checkbox_liability_cap",        sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("liability_cap_amount_sgd", sa.Float(), nullable=False),
        sa.Column("liability_cap_text_shown", sa.Text(), nullable=False),
        sa.Column("content_hash",       sa.String(64)),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("notarized_at",       sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("csp_id", "tos_version", name="uq_csp_tos_version"),
        sa.CheckConstraint(
            "checkbox_ai_disclaimer = TRUE AND checkbox_data_accuracy = TRUE AND "
            "checkbox_sanctions_limitation = TRUE AND checkbox_regulatory_change = TRUE AND "
            "checkbox_liability_cap = TRUE",
            name="chk_all_checkboxes_true",
        ),
    )
    op.create_index("ix_csp_tos_csp_id",  "csp_tos_acceptances", ["csp_id"])
    op.create_index("ix_csp_tos_user_id", "csp_tos_acceptances", ["user_id"])

    # csp_programme_attestations (Intervento 1) — all three declarations must be TRUE
    op.create_table("csp_programme_attestations",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("programme_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_aml_programme.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("csp_id",       postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approved_by",  sa.String(255), nullable=False),
        sa.Column("approved_at",  sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("declaration_content_accurate",        sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("declaration_legal_advice_considered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("declaration_sole_responsible",        sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("declaration_text_shown", sa.Text(), nullable=False),
        sa.Column("content_hash",       sa.String(64)),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("notarized_at",       sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "declaration_content_accurate = TRUE AND "
            "declaration_legal_advice_considered = TRUE AND "
            "declaration_sole_responsible = TRUE",
            name="chk_all_declarations_true",
        ),
    )
    op.create_index("ix_prog_att_csp_id", "csp_programme_attestations", ["csp_id"])

    # csp_risk_classification_audits (Intervento 2)
    op.create_table("csp_risk_classification_audits",
        sa.Column("id",            postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("csp_id",        postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id",     postgresql.UUID(as_uuid=True), sa.ForeignKey("csp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("classified_by", sa.String(255), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("risk_rating_assigned", sa.String(20), nullable=False),
        sa.Column("risk_rating_previous", sa.String(20)),
        sa.Column("risk_rationale",       sa.Text()),
        sa.Column("is_pep_at_classification",   sa.Boolean(), server_default="false"),
        sa.Column("high_risk_country_at_class", sa.Boolean(), server_default="false"),
        sa.Column("sanctions_clear_at_class",   sa.Boolean()),
        sa.Column("edd_required_at_class",      sa.Boolean(), server_default="false"),
        sa.Column("additional_risk_flags",      postgresql.JSONB()),
        sa.Column("content_hash",       sa.String(64)),
        sa.Column("blockchain_tx_hash", sa.String(66)),
        sa.Column("polygonscan_url",    sa.String(500)),
        sa.Column("notarized_at",       sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_risk_audit_client", "csp_risk_classification_audits", ["client_id"])
    op.create_index("ix_risk_audit_csp",    "csp_risk_classification_audits", ["csp_id"])
    op.create_index("ix_risk_audit_date",   "csp_risk_classification_audits", ["classified_at"])


def downgrade() -> None:
    for table in [
        "csp_risk_classification_audits",
        "csp_programme_attestations",
        "csp_tos_acceptances",
        "csp_blockchain_evidence",
        "csp_staff_training",
        "csp_compliance_calendar",
        "csp_risk_assessments",
        "csp_aml_programme",
        "csp_beneficial_owners",
        "csp_nominee_shareholders",
        "csp_nominee_directors",
        "csp_str_reports",
        "csp_edd_records",
        "csp_cdd_records",
        "csp_clients",
        "csp_profiles",
        "csp_org_memberships",
        "csp_organisations",
    ]:
        op.drop_table(table)
