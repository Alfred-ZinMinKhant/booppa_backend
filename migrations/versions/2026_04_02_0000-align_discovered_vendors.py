"""Align discovered_vendors schema with model

Revision ID: 2026_04_02_0000
Revises: 2026_04_01_0001
Create Date: 2026-04-02 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


# revision identifiers, used by Alembic.
revision = '2026_04_02_0000'
down_revision = '2026_04_01_0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Rename columns
    op.alter_column('discovered_vendors', 'sector', new_column_name='industry')
    op.alter_column('discovered_vendors', 'claimed_by', new_column_name='claimed_by_user_id')

    # 2. Add missing columns
    op.add_column('discovered_vendors', sa.Column('uen', sa.String(length=50), nullable=True))
    op.add_column('discovered_vendors', sa.Column('entity_type', sa.String(length=100), nullable=True))
    op.add_column('discovered_vendors', sa.Column('registration_date', sa.String(length=50), nullable=True))
    op.add_column('discovered_vendors', sa.Column('country', sa.String(length=100), server_default='Singapore', nullable=False))
    op.add_column('discovered_vendors', sa.Column('city', sa.String(length=100), nullable=True))
    op.add_column('discovered_vendors', sa.Column('gebiz_supplier', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('discovered_vendors', sa.Column('gebiz_contracts_count', sa.Integer(), server_default='0', nullable=False))
    op.add_column('discovered_vendors', sa.Column('gebiz_total_value', sa.Float(), server_default='0.0', nullable=False))
    op.add_column('discovered_vendors', sa.Column('source', sa.String(length=50), nullable=True)) # Temporarily nullable to avoid errors during upgrade if data exists
    op.add_column('discovered_vendors', sa.Column('source_data', sa.JSON(), nullable=True))
    op.add_column('discovered_vendors', sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False))

    # set source for existing data before making it non-nullable (if any)
    op.execute("UPDATE discovered_vendors SET source = 'gebiz' WHERE source IS NULL")
    op.alter_column('discovered_vendors', 'source', nullable=False)

    # 3. Add indexes and constraints
    op.create_index('ix_discovered_vendors_company_name', 'discovered_vendors', ['company_name'])
    op.create_index('ix_discovered_vendors_uen', 'discovered_vendors', ['uen'], unique=True)
    op.create_index('ix_discovered_vendors_industry', 'discovered_vendors', ['industry'])
    op.create_index('ix_discovered_vendors_domain', 'discovered_vendors', ['domain'])
    op.create_index('ix_discovered_vendors_claimed_by_user_id', 'discovered_vendors', ['claimed_by_user_id'])
    op.create_index('ix_discovered_vendors_created_at', 'discovered_vendors', ['created_at'])

    op.create_foreign_key(
        'fk_discovered_vendors_claimed_by_user_id',
        'discovered_vendors', 'users',
        ['claimed_by_user_id'], ['id'],
        ondelete='SET NULL'
    )

    # 4. Drop obsolete columns
    op.drop_column('discovered_vendors', 'scan_status')
    op.drop_column('discovered_vendors', 'claim_token')
    op.drop_column('discovered_vendors', 'last_scan_at')


def downgrade() -> None:
    # 1. Add back obsolete columns
    op.add_column('discovered_vendors', sa.Column('last_scan_at', sa.DateTime(), nullable=True))
    op.add_column('discovered_vendors', sa.Column('claim_token', sa.String(length=100), nullable=True))
    op.add_column('discovered_vendors', sa.Column('scan_status', sa.String(length=20), server_default='SCANNING', nullable=True))

    # 2. Drop foreign key and indexes
    op.drop_constraint('fk_discovered_vendors_claimed_by_user_id', 'discovered_vendors', type_='foreignkey')
    op.drop_index('ix_discovered_vendors_created_at', table_name='discovered_vendors')
    op.drop_index('ix_discovered_vendors_claimed_by_user_id', table_name='discovered_vendors')
    op.drop_index('ix_discovered_vendors_domain', table_name='discovered_vendors')
    op.drop_index('ix_discovered_vendors_industry', table_name='discovered_vendors')
    op.drop_index('ix_discovered_vendors_uen', table_name='discovered_vendors')
    op.drop_index('ix_discovered_vendors_company_name', table_name='discovered_vendors')

    # 3. Drop new columns
    op.drop_column('discovered_vendors', 'updated_at')
    op.drop_column('discovered_vendors', 'source_data')
    op.drop_column('discovered_vendors', 'source')
    op.drop_column('discovered_vendors', 'gebiz_total_value')
    op.drop_column('discovered_vendors', 'gebiz_contracts_count')
    op.drop_column('discovered_vendors', 'gebiz_supplier')
    op.drop_column('discovered_vendors', 'city')
    op.drop_column('discovered_vendors', 'country')
    op.drop_column('discovered_vendors', 'registration_date')
    op.drop_column('discovered_vendors', 'entity_type')
    op.drop_column('discovered_vendors', 'uen')

    # 4. Rename back columns
    op.alter_column('discovered_vendors', 'claimed_by_user_id', new_column_name='claimed_by')
    op.alter_column('discovered_vendors', 'industry', new_column_name='sector')
