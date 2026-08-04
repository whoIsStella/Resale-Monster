"""Add milestone four resale-domain tables and inventory fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0004"
down_revision: str | None = "20260802_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('category_completeness_rules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('required_fields', sa.JSON(), nullable=False),
    sa.Column('recommended_fields', sa.JSON(), nullable=False),
    sa.Column('required_measurements', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_category_completeness_rules')),
    sa.UniqueConstraint('category', 'version', name='category_version')
    )
    op.create_table('domain_proposals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('proposal_type', sa.Enum('inventory_update', 'identification_selection', 'pricing_change', 'listing_draft_approval', 'listing_variant_approval', 'archive_recommendation', 'research_resolution', name='proposal_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('resource_type', sa.String(length=100), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('proposed_payload', sa.JSON(), nullable=False),
    sa.Column('justification', sa.Text(), nullable=False),
    sa.Column('risk_tier', sa.String(length=50), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.Enum('pending', 'approved', 'rejected', 'expired', 'superseded', 'executed', 'execution_failed', name='proposal_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('reviewer', sa.String(length=200), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('review_notes', sa.Text(), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('execution_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_domain_proposals'))
    )
    op.create_index('ix_domain_proposals_status_type', 'domain_proposals', ['status', 'proposal_type'], unique=False)
    op.create_table('marketplace_constraint_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('marketplace', sa.Enum('ebay', 'poshmark', 'depop', 'mercari', 'grailed', 'facebook', 'generic', name='constraint_marketplace', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('max_title_length', sa.Integer(), nullable=False),
    sa.Column('required_fields', sa.JSON(), nullable=False),
    sa.Column('allowed_conditions', sa.JSON(), nullable=False),
    sa.Column('category_attributes', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_constraint_versions')),
    sa.UniqueConstraint('marketplace', 'version', name='marketplace_version')
    )
    op.create_table('mcp_service_principals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('credential_prefix', sa.String(length=16), nullable=False),
    sa.Column('credential_hash', sa.String(length=128), nullable=False),
    sa.Column('scopes', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_service_principals')),
    sa.UniqueConstraint('credential_hash', name='mcp_credential_hash'),
    sa.UniqueConstraint('name', name=op.f('uq_mcp_service_principals_name'))
    )
    op.create_table('comparable_sales',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('marketplace', sa.Enum('ebay', 'poshmark', 'depop', 'mercari', 'grailed', 'facebook', 'generic', name='comparable_marketplace', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('source_identity', sa.String(length=300), nullable=False),
    sa.Column('listing_title', sa.String(length=500), nullable=False),
    sa.Column('is_sold', sa.Boolean(), nullable=False),
    sa.Column('listed_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('sold_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('shipping_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('condition', sa.String(length=100), nullable=True),
    sa.Column('size', sa.String(length=50), nullable=True),
    sa.Column('sale_date', sa.Date(), nullable=True),
    sa.Column('source_url', sa.String(length=2000), nullable=True),
    sa.Column('similarity_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('reliability_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidation_reason', sa.Text(), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_comparable_sales_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_comparable_sales')),
    sa.UniqueConstraint('inventory_item_id', 'marketplace', 'source_identity', name='comp_identity')
    )
    op.create_index(op.f('ix_comparable_sales_inventory_item_id'), 'comparable_sales', ['inventory_item_id'], unique=False)
    op.create_index('ix_comparable_sales_item', 'comparable_sales', ['inventory_item_id'], unique=False)
    op.create_table('inventory_media',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('file_identifier', sa.String(length=200), nullable=False),
    sa.Column('media_type', sa.String(length=100), nullable=False),
    sa.Column('checksum', sa.String(length=128), nullable=False),
    sa.Column('file_size', sa.Integer(), nullable=False),
    sa.Column('image_width', sa.Integer(), nullable=True),
    sa.Column('image_height', sa.Integer(), nullable=True),
    sa.Column('role', sa.Enum('original', 'processed', 'label', 'defect', 'measurement', 'receipt', 'document', name='media_role', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('original_filename', sa.String(length=500), nullable=True),
    sa.Column('storage_key', sa.String(length=1000), nullable=False),
    sa.Column('processing_status', sa.Enum('pending', 'processing', 'complete', 'error', name='media_processing_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('processing_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('file_size >= 0', name=op.f('ck_inventory_media_file_size_nonnegative')),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_inventory_media_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_media')),
    sa.UniqueConstraint('inventory_item_id', 'checksum', name='inventory_item_checksum')
    )
    op.create_index(op.f('ix_inventory_media_inventory_item_id'), 'inventory_media', ['inventory_item_id'], unique=False)
    op.create_index('ix_inventory_media_item', 'inventory_media', ['inventory_item_id', 'role'], unique=False)
    op.create_table('measurements',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('measurement_type', sa.String(length=100), nullable=False),
    sa.Column('value', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('unit', sa.Enum('cm', 'in', name='measurement_unit', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('value_cm', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('method', sa.String(length=100), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('confidence is null or (confidence >= 0 and confidence <= 1)', name=op.f('ck_measurements_confidence_range')),
    sa.CheckConstraint('value > 0', name=op.f('ck_measurements_value_positive')),
    sa.CheckConstraint('value_cm > 0', name=op.f('ck_measurements_value_cm_positive')),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_measurements_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_measurements'))
    )
    op.create_index(op.f('ix_measurements_inventory_item_id'), 'measurements', ['inventory_item_id'], unique=False)
    op.create_index('ix_measurements_item', 'measurements', ['inventory_item_id', 'measurement_type'], unique=False)
    op.create_table('pricing_recommendations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('fee_version', sa.String(length=50), nullable=False),
    sa.Column('recommended_price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('fast_sale_price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('minimum_price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('expected_net_proceeds', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('expected_profit', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('expected_margin', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.Column('breakdown', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_pricing_recommendations_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pricing_recommendations'))
    )
    op.create_index(op.f('ix_pricing_recommendations_inventory_item_id'), 'pricing_recommendations', ['inventory_item_id'], unique=False)
    op.create_index('ix_pricing_recommendations_item', 'pricing_recommendations', ['inventory_item_id'], unique=False)
    op.create_table('proposal_evidence',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('proposal_id', sa.Uuid(), nullable=False),
    sa.Column('evidence_type', sa.String(length=100), nullable=False),
    sa.Column('reference_type', sa.String(length=100), nullable=False),
    sa.Column('reference_id', sa.Uuid(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['proposal_id'], ['domain_proposals.id'], name=op.f('fk_proposal_evidence_proposal_id_domain_proposals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proposal_evidence'))
    )
    op.create_index('ix_proposal_evidence_proposal', 'proposal_evidence', ['proposal_id'], unique=False)
    op.create_index(op.f('ix_proposal_evidence_proposal_id'), 'proposal_evidence', ['proposal_id'], unique=False)
    op.create_table('research_records',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('research_question', sa.Text(), nullable=False),
    sa.Column('researcher', sa.String(length=200), nullable=False),
    sa.Column('search_terms', sa.JSON(), nullable=False),
    sa.Column('observations', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('selected_identification', sa.JSON(), nullable=True),
    sa.Column('unresolved_questions', sa.JSON(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'in_progress', 'completed', 'inconclusive', 'rejected', name='research_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('confidence is null or (confidence >= 0 and confidence <= 1)', name=op.f('ck_research_records_confidence_range')),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_research_records_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_research_records'))
    )
    op.create_index(op.f('ix_research_records_inventory_item_id'), 'research_records', ['inventory_item_id'], unique=False)
    op.create_index('ix_research_records_item_status', 'research_records', ['inventory_item_id', 'status'], unique=False)
    op.create_table('identification_candidates',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('research_id', sa.Uuid(), nullable=False),
    sa.Column('brand', sa.String(length=200), nullable=True),
    sa.Column('model_name', sa.String(length=200), nullable=True),
    sa.Column('attributes', sa.JSON(), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('evidence_source_ids', sa.JSON(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('confidence is null or (confidence >= 0 and confidence <= 1)', name=op.f('ck_identification_candidates_confidence_range')),
    sa.ForeignKeyConstraint(['research_id'], ['research_records.id'], name=op.f('fk_identification_candidates_research_id_research_records'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_identification_candidates'))
    )
    op.create_index('ix_identification_candidates_research', 'identification_candidates', ['research_id'], unique=False)
    op.create_index(op.f('ix_identification_candidates_research_id'), 'identification_candidates', ['research_id'], unique=False)
    op.create_table('master_listing_drafts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('brand', sa.String(length=200), nullable=True),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('subcategory', sa.String(length=100), nullable=True),
    sa.Column('condition', sa.String(length=50), nullable=True),
    sa.Column('condition_details', sa.Text(), nullable=True),
    sa.Column('size', sa.String(length=50), nullable=True),
    sa.Column('colors', sa.JSON(), nullable=False),
    sa.Column('materials', sa.JSON(), nullable=False),
    sa.Column('style_keywords', sa.JSON(), nullable=False),
    sa.Column('measurements', sa.JSON(), nullable=False),
    sa.Column('defects', sa.JSON(), nullable=False),
    sa.Column('pricing_recommendation_id', sa.Uuid(), nullable=True),
    sa.Column('image_order', sa.JSON(), nullable=False),
    sa.Column('shipping_assumptions', sa.JSON(), nullable=False),
    sa.Column('status', sa.Enum('draft', 'needs_review', 'approved', 'rejected', 'superseded', name='master_draft_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('validation_warnings', sa.JSON(), nullable=False),
    sa.Column('missing_fields', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('version > 0', name=op.f('ck_master_listing_drafts_version_positive')),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_master_listing_drafts_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['pricing_recommendation_id'], ['pricing_recommendations.id'], name=op.f('fk_master_listing_drafts_pricing_recommendation_id_pricing_recommendations'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_master_listing_drafts'))
    )
    op.create_index(op.f('ix_master_listing_drafts_inventory_item_id'), 'master_listing_drafts', ['inventory_item_id'], unique=False)
    op.create_index('ix_master_listing_drafts_item_status', 'master_listing_drafts', ['inventory_item_id', 'status'], unique=False)
    op.create_table('research_sources',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('research_id', sa.Uuid(), nullable=False),
    sa.Column('source_type', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('url', sa.String(length=2000), nullable=True),
    sa.Column('accessed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=True),
    sa.Column('relevance_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('reliability', sa.Enum('official', 'high', 'medium', 'low', 'unknown', name='source_reliability', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('content_hash', sa.String(length=128), nullable=False),
    sa.Column('is_duplicate', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('relevance_score is null or (relevance_score >= 0 and relevance_score <= 1)', name=op.f('ck_research_sources_relevance_range')),
    sa.ForeignKeyConstraint(['research_id'], ['research_records.id'], name=op.f('fk_research_sources_research_id_research_records'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_research_sources')),
    sa.UniqueConstraint('research_id', 'content_hash', name='research_content_hash')
    )
    op.create_index('ix_research_sources_research', 'research_sources', ['research_id'], unique=False)
    op.create_index(op.f('ix_research_sources_research_id'), 'research_sources', ['research_id'], unique=False)
    op.create_table('listing_draft_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('draft_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('draft', 'needs_review', 'approved', 'rejected', 'superseded', name='draft_version_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['draft_id'], ['master_listing_drafts.id'], name=op.f('fk_listing_draft_versions_draft_id_master_listing_drafts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_listing_draft_versions')),
    sa.UniqueConstraint('draft_id', 'version', name='draft_version')
    )
    op.create_index('ix_listing_draft_versions_draft', 'listing_draft_versions', ['draft_id'], unique=False)
    op.create_index(op.f('ix_listing_draft_versions_draft_id'), 'listing_draft_versions', ['draft_id'], unique=False)
    op.create_table('marketplace_draft_variants',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('draft_id', sa.Uuid(), nullable=False),
    sa.Column('marketplace', sa.Enum('ebay', 'poshmark', 'depop', 'mercari', 'grailed', 'facebook', 'generic', name='variant_marketplace', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('constraint_version', sa.String(length=50), nullable=False),
    sa.Column('marketplace_title', sa.String(length=500), nullable=False),
    sa.Column('marketplace_description', sa.Text(), nullable=True),
    sa.Column('category_mapping', sa.String(length=200), nullable=True),
    sa.Column('item_specifics', sa.JSON(), nullable=False),
    sa.Column('hashtags', sa.JSON(), nullable=False),
    sa.Column('proposed_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('shipping_profile', sa.String(length=200), nullable=True),
    sa.Column('image_order', sa.JSON(), nullable=False),
    sa.Column('validation_warnings', sa.JSON(), nullable=False),
    sa.Column('status', sa.Enum('draft', 'needs_review', 'approved', 'rejected', 'superseded', name='variant_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['draft_id'], ['master_listing_drafts.id'], name=op.f('fk_marketplace_draft_variants_draft_id_master_listing_drafts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_draft_variants')),
    sa.UniqueConstraint('draft_id', 'marketplace', name='variant_marketplace')
    )
    op.create_index('ix_marketplace_draft_variants_draft', 'marketplace_draft_variants', ['draft_id'], unique=False)
    op.create_index(op.f('ix_marketplace_draft_variants_draft_id'), 'marketplace_draft_variants', ['draft_id'], unique=False)
    op.add_column('inventory_items', sa.Column('brand', sa.String(length=200), nullable=True))
    op.add_column('inventory_items', sa.Column('model_name', sa.String(length=200), nullable=True))
    op.add_column('inventory_items', sa.Column('category', sa.String(length=100), nullable=True))
    op.add_column('inventory_items', sa.Column('subcategory', sa.String(length=100), nullable=True))
    op.add_column('inventory_items', sa.Column('department', sa.String(length=50), nullable=True))
    op.add_column('inventory_items', sa.Column('size_label', sa.String(length=50), nullable=True))
    op.add_column('inventory_items', sa.Column('normalized_size', sa.String(length=50), nullable=True))
    op.add_column('inventory_items', sa.Column('colors', sa.JSON(), nullable=False))
    op.add_column('inventory_items', sa.Column('materials', sa.JSON(), nullable=False))
    op.add_column('inventory_items', sa.Column('pattern', sa.String(length=100), nullable=True))
    op.add_column('inventory_items', sa.Column('condition_grade', sa.String(length=50), nullable=True))
    op.add_column('inventory_items', sa.Column('condition_notes', sa.Text(), nullable=True))
    op.add_column('inventory_items', sa.Column('defects', sa.JSON(), nullable=False))
    op.add_column('inventory_items', sa.Column('acquisition_date', sa.Date(), nullable=True))
    op.add_column('inventory_items', sa.Column('acquisition_source', sa.String(length=200), nullable=True))
    op.add_column('inventory_items', sa.Column('cost_basis', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('inventory_items', sa.Column('estimated_weight_grams', sa.Numeric(precision=12, scale=3), nullable=True))
    op.add_column('inventory_items', sa.Column('packed_weight_grams', sa.Numeric(precision=12, scale=3), nullable=True))
    op.add_column('inventory_items', sa.Column('package_dimensions', sa.JSON(), nullable=False))
    op.add_column('inventory_items', sa.Column('storage_location', sa.String(length=200), nullable=True))
    op.add_column('inventory_items', sa.Column('status', sa.Enum('draft', 'research_needed', 'ready_for_listing', 'listed', 'reserved', 'sold', 'archived', 'donated', 'lost', name='inventory_status', native_enum=False, create_constraint=True), nullable=False))
    op.add_column('inventory_items', sa.Column('research_confidence', sa.Numeric(precision=4, scale=3), nullable=True))
    op.add_column('inventory_items', sa.Column('identification_confidence', sa.Numeric(precision=4, scale=3), nullable=True))
    op.add_column('inventory_items', sa.Column('version', sa.Integer(), nullable=False))
    op.create_index('ix_inventory_items_status', 'inventory_items', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_inventory_items_status', table_name='inventory_items')
    op.drop_column('inventory_items', 'version')
    op.drop_column('inventory_items', 'identification_confidence')
    op.drop_column('inventory_items', 'research_confidence')
    op.drop_column('inventory_items', 'status')
    op.drop_column('inventory_items', 'storage_location')
    op.drop_column('inventory_items', 'package_dimensions')
    op.drop_column('inventory_items', 'packed_weight_grams')
    op.drop_column('inventory_items', 'estimated_weight_grams')
    op.drop_column('inventory_items', 'cost_basis')
    op.drop_column('inventory_items', 'acquisition_source')
    op.drop_column('inventory_items', 'acquisition_date')
    op.drop_column('inventory_items', 'defects')
    op.drop_column('inventory_items', 'condition_notes')
    op.drop_column('inventory_items', 'condition_grade')
    op.drop_column('inventory_items', 'pattern')
    op.drop_column('inventory_items', 'materials')
    op.drop_column('inventory_items', 'colors')
    op.drop_column('inventory_items', 'normalized_size')
    op.drop_column('inventory_items', 'size_label')
    op.drop_column('inventory_items', 'department')
    op.drop_column('inventory_items', 'subcategory')
    op.drop_column('inventory_items', 'category')
    op.drop_column('inventory_items', 'model_name')
    op.drop_column('inventory_items', 'brand')
    op.drop_index(op.f('ix_marketplace_draft_variants_draft_id'), table_name='marketplace_draft_variants')
    op.drop_index('ix_marketplace_draft_variants_draft', table_name='marketplace_draft_variants')
    op.drop_table('marketplace_draft_variants')
    op.drop_index(op.f('ix_listing_draft_versions_draft_id'), table_name='listing_draft_versions')
    op.drop_index('ix_listing_draft_versions_draft', table_name='listing_draft_versions')
    op.drop_table('listing_draft_versions')
    op.drop_index(op.f('ix_research_sources_research_id'), table_name='research_sources')
    op.drop_index('ix_research_sources_research', table_name='research_sources')
    op.drop_table('research_sources')
    op.drop_index('ix_master_listing_drafts_item_status', table_name='master_listing_drafts')
    op.drop_index(op.f('ix_master_listing_drafts_inventory_item_id'), table_name='master_listing_drafts')
    op.drop_table('master_listing_drafts')
    op.drop_index(op.f('ix_identification_candidates_research_id'), table_name='identification_candidates')
    op.drop_index('ix_identification_candidates_research', table_name='identification_candidates')
    op.drop_table('identification_candidates')
    op.drop_index('ix_research_records_item_status', table_name='research_records')
    op.drop_index(op.f('ix_research_records_inventory_item_id'), table_name='research_records')
    op.drop_table('research_records')
    op.drop_index(op.f('ix_proposal_evidence_proposal_id'), table_name='proposal_evidence')
    op.drop_index('ix_proposal_evidence_proposal', table_name='proposal_evidence')
    op.drop_table('proposal_evidence')
    op.drop_index('ix_pricing_recommendations_item', table_name='pricing_recommendations')
    op.drop_index(op.f('ix_pricing_recommendations_inventory_item_id'), table_name='pricing_recommendations')
    op.drop_table('pricing_recommendations')
    op.drop_index('ix_measurements_item', table_name='measurements')
    op.drop_index(op.f('ix_measurements_inventory_item_id'), table_name='measurements')
    op.drop_table('measurements')
    op.drop_index('ix_inventory_media_item', table_name='inventory_media')
    op.drop_index(op.f('ix_inventory_media_inventory_item_id'), table_name='inventory_media')
    op.drop_table('inventory_media')
    op.drop_index('ix_comparable_sales_item', table_name='comparable_sales')
    op.drop_index(op.f('ix_comparable_sales_inventory_item_id'), table_name='comparable_sales')
    op.drop_table('comparable_sales')
    op.drop_table('mcp_service_principals')
    op.drop_table('marketplace_constraint_versions')
    op.drop_index('ix_domain_proposals_status_type', table_name='domain_proposals')
    op.drop_table('domain_proposals')
    op.drop_table('category_completeness_rules')
