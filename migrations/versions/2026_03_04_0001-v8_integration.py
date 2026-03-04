"""v8_integration — VendorStatusSnapshot, ScoreSnapshot, NotarizationMetadata,
RfpRequirement, RfpRequirementFlag + vendor_sectors.is_primary column.

Revision ID: 2026_03_04_0001
Revises: 6ae5998fc1ab
Create Date: 2026-03-04 09:35:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '2026_03_04_0001'
down_revision = '6ae5998fc1ab'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── VendorStatusSnapshot ──────────────────────────────────────────────────
    op.create_table(
        'vendor_status_snapshots',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('verification_depth', sa.String(length=50), nullable=False, server_default='UNVERIFIED'),
        sa.Column('monitoring_activity', sa.String(length=50), nullable=False, server_default='NONE'),
        sa.Column('risk_signal', sa.String(length=50), nullable=False, server_default='CLEAN'),
        sa.Column('procurement_readiness', sa.String(length=50), nullable=False, server_default='NOT_READY'),
        sa.Column('risk_adjusted_pct', sa.Float(), nullable=False, server_default='50'),
        sa.Column('dual_silent_mode', sa.String(length=50), nullable=False, server_default='SILENT_RISK_CAPTURE'),
        sa.Column('notarization_depth', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('confidence_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('version', sa.String(length=20), nullable=False, server_default='v2'),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id'),
    )
    op.create_index('ix_vendor_status_snapshots_vendor_id', 'vendor_status_snapshots', ['vendor_id'], unique=True)
    op.create_index('ix_vendor_status_snapshots_verification_depth', 'vendor_status_snapshots', ['verification_depth'])
    op.create_index('ix_vendor_status_snapshots_monitoring_activity', 'vendor_status_snapshots', ['monitoring_activity'])
    op.create_index('ix_vendor_status_snapshots_risk_signal', 'vendor_status_snapshots', ['risk_signal'])
    op.create_index('ix_vendor_status_snapshots_procurement_readiness', 'vendor_status_snapshots', ['procurement_readiness'])
    op.create_index('ix_vendor_status_snapshots_computed_at', 'vendor_status_snapshots', ['computed_at'])

    # ── ScoreSnapshot ─────────────────────────────────────────────────────────
    op.create_table(
        'score_snapshots',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('base_score', sa.Float(), nullable=False),
        sa.Column('multiplier', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('final_score', sa.Integer(), nullable=False),
        sa.Column('breakdown', sa.JSON(), nullable=True),
        sa.Column('sector_percentile', sa.Float(), nullable=False, server_default='50'),
        sa.Column('score_version', sa.String(length=20), nullable=True),
        sa.Column('score_hash', sa.String(length=64), nullable=True),
        sa.Column('quarter', sa.String(length=20), nullable=True),
        sa.Column('snapshot_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_score_snapshots_vendor_id', 'score_snapshots', ['vendor_id'])
    op.create_index('ix_score_snapshots_snapshot_at', 'score_snapshots', ['snapshot_at'])
    op.create_index('ix_score_snapshots_vendor_snapshot', 'score_snapshots', ['vendor_id', 'snapshot_at'])
    op.create_index('ix_score_snapshots_final_score', 'score_snapshots', ['final_score'])
    op.create_index('ix_score_snapshots_quarter', 'score_snapshots', ['quarter'])
    op.create_index('ix_score_snapshots_score_hash', 'score_snapshots', ['score_hash'])

    # ── NotarizationMetadata ──────────────────────────────────────────────────
    op.create_table(
        'notarization_metadata',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('notarized_at', sa.DateTime(), nullable=False),
        sa.Column('validation_id', sa.String(length=64), nullable=False),
        sa.Column('verification_depth', sa.String(length=50), nullable=False),
        sa.Column('structural_level', sa.String(length=50), nullable=False, server_default='ELEVATED'),
        sa.Column('public_hash', sa.String(length=64), nullable=False),
        sa.Column('logic_version', sa.String(length=20), nullable=False),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('evidence_count_by_sector', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('confidence_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id'),
        sa.UniqueConstraint('validation_id'),
    )
    op.create_index('ix_notarization_metadata_vendor_id', 'notarization_metadata', ['vendor_id'], unique=True)
    op.create_index('ix_notarization_metadata_structural_level', 'notarization_metadata', ['structural_level'])
    op.create_index('ix_notarization_metadata_verification_depth', 'notarization_metadata', ['verification_depth'])
    op.create_index('ix_notarization_metadata_notarized_at', 'notarization_metadata', ['notarized_at'])
    op.create_index('ix_notarization_metadata_structural_confidence', 'notarization_metadata', ['structural_level', 'confidence_score'])
    op.create_index('ix_notarization_metadata_structural_evidence', 'notarization_metadata', ['structural_level', 'evidence_count'])

    # ── RfpRequirement ────────────────────────────────────────────────────────
    op.create_table(
        'rfp_requirements',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_by_user_id', sa.UUID(), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('minimum_verification_depth', sa.String(length=50), nullable=False, server_default='NONE'),
        sa.Column('minimum_percentile', sa.Float(), nullable=False, server_default='0'),
        sa.Column('require_active_monitoring', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('require_no_open_anomalies', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('minimum_days_until_expiry', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rfp_requirements_created_by_user_id', 'rfp_requirements', ['created_by_user_id'])
    op.create_index('ix_rfp_requirements_archived', 'rfp_requirements', ['archived'])
    op.create_index('ix_rfp_requirements_created_at', 'rfp_requirements', ['created_at'])

    # ── RfpRequirementFlag ────────────────────────────────────────────────────
    op.create_table(
        'rfp_requirement_flags',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('requirement_id', sa.UUID(), nullable=False),
        sa.Column('overall_status', sa.String(length=20), nullable=False),
        sa.Column('flag_details', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('evaluated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requirement_id'], ['rfp_requirements.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id', 'requirement_id', name='uq_rfp_flag_vendor_requirement'),
    )
    op.create_index('ix_rfp_requirement_flags_vendor_id', 'rfp_requirement_flags', ['vendor_id'])
    op.create_index('ix_rfp_requirement_flags_requirement_id', 'rfp_requirement_flags', ['requirement_id'])
    op.create_index('ix_rfp_requirement_flags_overall_status', 'rfp_requirement_flags', ['overall_status'])
    op.create_index('ix_rfp_requirement_flags_evaluated_at', 'rfp_requirement_flags', ['evaluated_at'])

    # ── Alter vendor_sectors: add is_primary column ───────────────────────────
    op.add_column('vendor_sectors', sa.Column('is_primary', sa.Boolean(), nullable=False, server_default='false'))
    op.create_index('ix_vendor_sectors_is_primary', 'vendor_sectors', ['is_primary'])


def downgrade() -> None:
    # Drop in reverse order of creation
    op.drop_index('ix_vendor_sectors_is_primary', table_name='vendor_sectors')
    op.drop_column('vendor_sectors', 'is_primary')

    op.drop_index('ix_rfp_requirement_flags_evaluated_at', table_name='rfp_requirement_flags')
    op.drop_index('ix_rfp_requirement_flags_overall_status', table_name='rfp_requirement_flags')
    op.drop_index('ix_rfp_requirement_flags_requirement_id', table_name='rfp_requirement_flags')
    op.drop_index('ix_rfp_requirement_flags_vendor_id', table_name='rfp_requirement_flags')
    op.drop_table('rfp_requirement_flags')

    op.drop_index('ix_rfp_requirements_created_at', table_name='rfp_requirements')
    op.drop_index('ix_rfp_requirements_archived', table_name='rfp_requirements')
    op.drop_index('ix_rfp_requirements_created_by_user_id', table_name='rfp_requirements')
    op.drop_table('rfp_requirements')

    op.drop_index('ix_notarization_metadata_structural_evidence', table_name='notarization_metadata')
    op.drop_index('ix_notarization_metadata_structural_confidence', table_name='notarization_metadata')
    op.drop_index('ix_notarization_metadata_notarized_at', table_name='notarization_metadata')
    op.drop_index('ix_notarization_metadata_verification_depth', table_name='notarization_metadata')
    op.drop_index('ix_notarization_metadata_structural_level', table_name='notarization_metadata')
    op.drop_index('ix_notarization_metadata_vendor_id', table_name='notarization_metadata')
    op.drop_table('notarization_metadata')

    op.drop_index('ix_score_snapshots_score_hash', table_name='score_snapshots')
    op.drop_index('ix_score_snapshots_quarter', table_name='score_snapshots')
    op.drop_index('ix_score_snapshots_final_score', table_name='score_snapshots')
    op.drop_index('ix_score_snapshots_vendor_snapshot', table_name='score_snapshots')
    op.drop_index('ix_score_snapshots_snapshot_at', table_name='score_snapshots')
    op.drop_index('ix_score_snapshots_vendor_id', table_name='score_snapshots')
    op.drop_table('score_snapshots')

    op.drop_index('ix_vendor_status_snapshots_computed_at', table_name='vendor_status_snapshots')
    op.drop_index('ix_vendor_status_snapshots_procurement_readiness', table_name='vendor_status_snapshots')
    op.drop_index('ix_vendor_status_snapshots_risk_signal', table_name='vendor_status_snapshots')
    op.drop_index('ix_vendor_status_snapshots_monitoring_activity', table_name='vendor_status_snapshots')
    op.drop_index('ix_vendor_status_snapshots_verification_depth', table_name='vendor_status_snapshots')
    op.drop_index('ix_vendor_status_snapshots_vendor_id', table_name='vendor_status_snapshots')
    op.drop_table('vendor_status_snapshots')
