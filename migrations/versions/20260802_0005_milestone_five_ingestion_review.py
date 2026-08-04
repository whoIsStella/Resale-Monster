"""Add milestone five media, comparable ingestion, and review-task tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0005"
down_revision: str | None = "20260802_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('pricing_source_policies',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('reviewed_only', sa.Boolean(), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pricing_source_policies')),
    sa.UniqueConstraint('name', 'version', name='policy_name_version')
    )
    op.create_table('review_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task_type', sa.Enum('inventory_completion', 'media_validation', 'image_quality', 'research_resolution', 'comparable_review', 'pricing_review', 'listing_review', 'proposal_review', name='review_task_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('resource_type', sa.String(length=100), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=300), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('open', 'claimed', 'completed', 'dismissed', 'expired', name='review_task_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('assigned_reviewer', sa.String(length=200), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=100), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('version > 0', name=op.f('ck_review_tasks_version_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_tasks')),
    sa.UniqueConstraint('dedupe_key', name='review_task_dedupe')
    )
    op.create_index('ix_review_tasks_status_type', 'review_tasks', ['status', 'task_type'], unique=False)
    op.create_table('comparable_imports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('source_format', sa.String(length=20), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'dry_run', 'completed', 'rolled_back', 'failed', name='import_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('total_rows', sa.Integer(), nullable=False),
    sa.Column('imported_rows', sa.Integer(), nullable=False),
    sa.Column('failed_rows', sa.Integer(), nullable=False),
    sa.Column('duplicate_rows', sa.Integer(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_comparable_imports_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_comparable_imports'))
    )
    op.create_index(op.f('ix_comparable_imports_inventory_item_id'), 'comparable_imports', ['inventory_item_id'], unique=False)
    op.create_index('ix_comparable_imports_item', 'comparable_imports', ['inventory_item_id'], unique=False)
    op.create_table('review_task_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['review_tasks.id'], name=op.f('fk_review_task_events_task_id_review_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_task_events'))
    )
    op.create_index('ix_review_task_events_task', 'review_task_events', ['task_id'], unique=False)
    op.create_index(op.f('ix_review_task_events_task_id'), 'review_task_events', ['task_id'], unique=False)
    op.create_table('comparable_import_rows',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('import_id', sa.Uuid(), nullable=False),
    sa.Column('row_number', sa.Integer(), nullable=False),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('comparable_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['import_id'], ['comparable_imports.id'], name=op.f('fk_comparable_import_rows_import_id_comparable_imports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_comparable_import_rows'))
    )
    op.create_index('ix_comparable_import_rows_import', 'comparable_import_rows', ['import_id'], unique=False)
    op.create_index(op.f('ix_comparable_import_rows_import_id'), 'comparable_import_rows', ['import_id'], unique=False)
    op.create_table('comparable_review_decisions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('comparable_id', sa.Uuid(), nullable=False),
    sa.Column('reviewer', sa.String(length=200), nullable=False),
    sa.Column('decision', sa.Enum('pending_review', 'accepted', 'rejected', 'duplicate', 'invalidated', name='review_decision_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('similarity_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('reliability_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['comparable_id'], ['comparable_sales.id'], name=op.f('fk_comparable_review_decisions_comparable_id_comparable_sales'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_comparable_review_decisions'))
    )
    op.create_index('ix_comparable_review_decisions_comparable', 'comparable_review_decisions', ['comparable_id'], unique=False)
    op.create_index(op.f('ix_comparable_review_decisions_comparable_id'), 'comparable_review_decisions', ['comparable_id'], unique=False)
    op.create_table('image_quality_results',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('media_id', sa.Uuid(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('findings', sa.JSON(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('blur_variance', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('aspect_ratio', sa.Numeric(precision=8, scale=3), nullable=True),
    sa.Column('color_mode', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['media_id'], ['inventory_media.id'], name=op.f('fk_image_quality_results_media_id_inventory_media'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_image_quality_results'))
    )
    op.create_index('ix_image_quality_results_media', 'image_quality_results', ['media_id'], unique=False)
    op.create_index(op.f('ix_image_quality_results_media_id'), 'image_quality_results', ['media_id'], unique=False)
    op.create_table('media_derivations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('parent_media_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.String(length=100), nullable=False),
    sa.Column('storage_key', sa.String(length=1000), nullable=False),
    sa.Column('media_type', sa.String(length=100), nullable=False),
    sa.Column('checksum', sa.String(length=128), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('file_size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_media_id'], ['inventory_media.id'], name=op.f('fk_media_derivations_parent_media_id_inventory_media'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media_derivations')),
    sa.UniqueConstraint('parent_media_id', 'operation', name='derivation_operation')
    )
    op.create_index('ix_media_derivations_parent', 'media_derivations', ['parent_media_id'], unique=False)
    op.create_index(op.f('ix_media_derivations_parent_media_id'), 'media_derivations', ['parent_media_id'], unique=False)
    op.create_table('media_processing_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('media_id', sa.Uuid(), nullable=False),
    sa.Column('job_type', sa.String(length=50), nullable=False),
    sa.Column('status', sa.Enum('queued', 'running', 'succeeded', 'failed', name='media_job_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('error_category', sa.String(length=100), nullable=True),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column('locked_by', sa.String(length=200), nullable=True),
    sa.Column('lease_token_hash', sa.String(length=128), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_eligible_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_media_processing_jobs_attempts_nonnegative')),
    sa.CheckConstraint('version > 0', name=op.f('ck_media_processing_jobs_version_positive')),
    sa.ForeignKeyConstraint(['media_id'], ['inventory_media.id'], name=op.f('fk_media_processing_jobs_media_id_inventory_media'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media_processing_jobs'))
    )
    op.create_index(op.f('ix_media_processing_jobs_media_id'), 'media_processing_jobs', ['media_id'], unique=False)
    op.create_index('ix_media_processing_jobs_status', 'media_processing_jobs', ['status', 'next_eligible_at'], unique=False)
    op.create_table('perceptual_hashes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('media_id', sa.Uuid(), nullable=False),
    sa.Column('algorithm', sa.String(length=20), nullable=False),
    sa.Column('hash_hex', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['media_id'], ['inventory_media.id'], name=op.f('fk_perceptual_hashes_media_id_inventory_media'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_perceptual_hashes')),
    sa.UniqueConstraint('media_id', 'algorithm', name='perceptual_media_algorithm')
    )
    op.create_index(op.f('ix_perceptual_hashes_media_id'), 'perceptual_hashes', ['media_id'], unique=False)
    op.create_index('ix_perceptual_hashes_value', 'perceptual_hashes', ['algorithm', 'hash_hex'], unique=False)
    op.create_table('media_processing_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('error_category', sa.String(length=100), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['media_processing_jobs.id'], name=op.f('fk_media_processing_attempts_job_id_media_processing_jobs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media_processing_attempts')),
    sa.UniqueConstraint('job_id', 'attempt_number', name='media_attempt_number')
    )
    op.create_index('ix_media_processing_attempts_job', 'media_processing_attempts', ['job_id'], unique=False)
    op.create_index(op.f('ix_media_processing_attempts_job_id'), 'media_processing_attempts', ['job_id'], unique=False)
    op.add_column('comparable_sales', sa.Column('provider_type', sa.Enum('imported_csv', 'manual', 'browser_agent', 'marketplace_api', 'data_provider', name='comparable_provider_type', native_enum=False, create_constraint=True), nullable=False))
    op.add_column('comparable_sales', sa.Column('provider_identity', sa.String(length=200), nullable=True))
    op.add_column('comparable_sales', sa.Column('review_status', sa.Enum('pending_review', 'accepted', 'rejected', 'duplicate', 'invalidated', name='comparable_review_status', native_enum=False, create_constraint=True), nullable=False))
    op.add_column('comparable_sales', sa.Column('reviewed_by', sa.String(length=200), nullable=True))
    op.add_column('comparable_sales', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('comparable_sales', sa.Column('import_id', sa.Uuid(), nullable=True))
    op.add_column('inventory_media', sa.Column('status', sa.Enum('pending', 'validated', 'quarantined', 'processing', 'processed', 'failed', 'archived', name='media_status', native_enum=False, create_constraint=True), nullable=False))
    op.add_column('inventory_media', sa.Column('detected_media_type', sa.String(length=100), nullable=True))
    op.add_column('inventory_media', sa.Column('validation_result', sa.JSON(), nullable=False))
    op.add_column('inventory_media', sa.Column('processing_attempts', sa.Integer(), nullable=False))
    op.add_column('inventory_media', sa.Column('processing_error_category', sa.String(length=100), nullable=True))
    op.add_column('inventory_media', sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('inventory_media', sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('inventory_media', sa.Column('processing_completed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('inventory_media', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('inventory_media', sa.Column('version', sa.Integer(), nullable=False))
    op.add_column('pricing_recommendations', sa.Column('policy_version', sa.String(length=50), nullable=True))
    op.add_column('pricing_recommendations', sa.Column('included_comparables', sa.JSON(), nullable=False))
    op.add_column('pricing_recommendations', sa.Column('excluded_comparables', sa.JSON(), nullable=False))


def downgrade() -> None:
    op.drop_column('pricing_recommendations', 'excluded_comparables')
    op.drop_column('pricing_recommendations', 'included_comparables')
    op.drop_column('pricing_recommendations', 'policy_version')
    op.drop_column('inventory_media', 'version')
    op.drop_column('inventory_media', 'archived_at')
    op.drop_column('inventory_media', 'processing_completed_at')
    op.drop_column('inventory_media', 'processing_started_at')
    op.drop_column('inventory_media', 'validated_at')
    op.drop_column('inventory_media', 'processing_error_category')
    op.drop_column('inventory_media', 'processing_attempts')
    op.drop_column('inventory_media', 'validation_result')
    op.drop_column('inventory_media', 'detected_media_type')
    op.drop_column('inventory_media', 'status')
    op.drop_column('comparable_sales', 'import_id')
    op.drop_column('comparable_sales', 'reviewed_at')
    op.drop_column('comparable_sales', 'reviewed_by')
    op.drop_column('comparable_sales', 'review_status')
    op.drop_column('comparable_sales', 'provider_identity')
    op.drop_column('comparable_sales', 'provider_type')
    op.drop_index(op.f('ix_media_processing_attempts_job_id'), table_name='media_processing_attempts')
    op.drop_index('ix_media_processing_attempts_job', table_name='media_processing_attempts')
    op.drop_table('media_processing_attempts')
    op.drop_index('ix_perceptual_hashes_value', table_name='perceptual_hashes')
    op.drop_index(op.f('ix_perceptual_hashes_media_id'), table_name='perceptual_hashes')
    op.drop_table('perceptual_hashes')
    op.drop_index('ix_media_processing_jobs_status', table_name='media_processing_jobs')
    op.drop_index(op.f('ix_media_processing_jobs_media_id'), table_name='media_processing_jobs')
    op.drop_table('media_processing_jobs')
    op.drop_index(op.f('ix_media_derivations_parent_media_id'), table_name='media_derivations')
    op.drop_index('ix_media_derivations_parent', table_name='media_derivations')
    op.drop_table('media_derivations')
    op.drop_index(op.f('ix_image_quality_results_media_id'), table_name='image_quality_results')
    op.drop_index('ix_image_quality_results_media', table_name='image_quality_results')
    op.drop_table('image_quality_results')
    op.drop_index(op.f('ix_comparable_review_decisions_comparable_id'), table_name='comparable_review_decisions')
    op.drop_index('ix_comparable_review_decisions_comparable', table_name='comparable_review_decisions')
    op.drop_table('comparable_review_decisions')
    op.drop_index(op.f('ix_comparable_import_rows_import_id'), table_name='comparable_import_rows')
    op.drop_index('ix_comparable_import_rows_import', table_name='comparable_import_rows')
    op.drop_table('comparable_import_rows')
    op.drop_index(op.f('ix_review_task_events_task_id'), table_name='review_task_events')
    op.drop_index('ix_review_task_events_task', table_name='review_task_events')
    op.drop_table('review_task_events')
    op.drop_index('ix_comparable_imports_item', table_name='comparable_imports')
    op.drop_index(op.f('ix_comparable_imports_inventory_item_id'), table_name='comparable_imports')
    op.drop_table('comparable_imports')
    op.drop_index('ix_review_tasks_status_type', table_name='review_tasks')
    op.drop_table('review_tasks')
    op.drop_table('pricing_source_policies')
