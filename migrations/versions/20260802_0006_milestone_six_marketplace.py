"""Add milestone six autonomous marketplace tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0006"
down_revision: str | None = "20260802_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('automation_policy_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('policy_type', sa.String(length=80), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_automation_policy_versions')),
    sa.UniqueConstraint('policy_type', 'version', name='policy_type_version')
    )
    op.create_table('circuit_breakers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scope', sa.String(length=50), nullable=False),
    sa.Column('scope_key', sa.String(length=200), nullable=False),
    sa.Column('state', sa.Enum('closed', 'open', 'half_open', name='circuit_breaker_state', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('threshold', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reset_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_probe_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_circuit_breakers')),
    sa.UniqueConstraint('scope', 'scope_key', name='breaker_scope')
    )
    op.create_index('ix_circuit_breakers_state', 'circuit_breakers', ['state'], unique=False)
    op.create_table('emergency_stop_state',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scope_key', sa.String(length=200), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('activated_by', sa.String(length=200), nullable=True),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_emergency_stop_state')),
    sa.UniqueConstraint('scope_key', name='emergency_stop_scope')
    )
    op.create_table('exception_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('exception_type', sa.String(length=100), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('resource_type', sa.String(length=80), nullable=True),
    sa.Column('resource_id', sa.Uuid(), nullable=True),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('status', sa.Enum('open', 'acknowledged', 'resolved', 'dismissed', name='exception_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('assigned_to', sa.String(length=200), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution_notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('version > 0', name=op.f('ck_exception_tasks_version_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exception_tasks'))
    )
    op.create_index('ix_exception_tasks_status', 'exception_tasks', ['status', 'severity'], unique=False)
    op.create_table('marketplace_accounts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('marketplace', sa.Enum('ebay', 'poshmark', 'depop', 'mercari', 'grailed', 'facebook', 'generic', name='account_marketplace', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('account_label', sa.String(length=100), nullable=False),
    sa.Column('seller_region', sa.String(length=50), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('automation_mode', sa.Enum('disabled', 'observe', 'shadow', 'autonomous_conservative', 'autonomous_normal', 'paused', name='account_automation_mode', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('disconnected', 'authentication_required', 'connecting', 'healthy', 'degraded', 'rate_limited', 'challenged', 'suspended', 'disabled', name='marketplace_account_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('session_reference', sa.String(length=200), nullable=True),
    sa.Column('session_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_authenticated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failed_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('rate_limit_state', sa.JSON(), nullable=False),
    sa.Column('health_state', sa.String(length=50), nullable=False),
    sa.Column('mode_version', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('consecutive_failures >= 0', name=op.f('ck_marketplace_accounts_failures_nonnegative')),
    sa.CheckConstraint('version > 0', name=op.f('ck_marketplace_accounts_version_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_accounts')),
    sa.UniqueConstraint('marketplace', 'account_label', name='account_marketplace_label')
    )
    op.create_index('ix_marketplace_accounts_status', 'marketplace_accounts', ['status'], unique=False)
    op.create_table('policy_decisions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('policy_type', sa.String(length=80), nullable=False),
    sa.Column('policy_version', sa.String(length=50), nullable=False),
    sa.Column('resource_type', sa.String(length=80), nullable=True),
    sa.Column('resource_id', sa.Uuid(), nullable=True),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('decision', sa.String(length=80), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('confidence_requirements', sa.JSON(), nullable=False),
    sa.Column('risk_score', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('execution_result', sa.String(length=80), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_decisions'))
    )
    op.create_index('ix_policy_decisions_type', 'policy_decisions', ['policy_type', 'created_at'], unique=False)
    op.create_table('synchronization_conflicts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conflict_type', sa.String(length=100), nullable=False),
    sa.Column('resource_type', sa.String(length=80), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=True),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('resolution_policy', sa.String(length=50), nullable=True),
    sa.Column('resolved', sa.Boolean(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_synchronization_conflicts'))
    )
    op.create_index('ix_sync_conflicts_status', 'synchronization_conflicts', ['resolved'], unique=False)
    op.create_table('webhook_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=80), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('payload_checksum', sa.String(length=128), nullable=False),
    sa.Column('processed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('source', 'external_id', name='webhook_identity')
    )
    op.create_index('ix_webhook_events_processed', 'webhook_events', ['processed'], unique=False)
    op.create_table('inventory_reservations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.Enum('pending_payment', 'paid_sale', 'active_checkout', 'accepted_offer', 'bundle_negotiation', 'marketplace_hold', 'suspected_duplicate_sale', 'manual_hold', name='reservation_reason', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('source_marketplace', sa.String(length=50), nullable=True),
    sa.Column('source_listing_id', sa.String(length=200), nullable=True),
    sa.Column('source_order_id', sa.String(length=200), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('release_reason', sa.String(length=200), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('version > 0', name=op.f('ck_inventory_reservations_version_positive')),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_inventory_reservations_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_reservations'))
    )
    op.create_index(op.f('ix_inventory_reservations_inventory_item_id'), 'inventory_reservations', ['inventory_item_id'], unique=False)
    op.create_index('ix_inventory_reservations_item', 'inventory_reservations', ['inventory_item_id'], unique=False)
    op.create_table('marketplace_offers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('remote_offer_id', sa.String(length=200), nullable=False),
    sa.Column('remote_listing_id', sa.String(length=200), nullable=True),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.Column('buyer_reference', sa.String(length=64), nullable=True),
    sa.Column('offer_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('list_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.Enum('pending', 'accepted', 'declined', 'countered', 'expired', 'escalated', name='offer_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('decision', sa.Enum('accept', 'decline', 'counter', 'send_offer', 'defer', 'escalate', name='offer_decision', native_enum=False, create_constraint=True), nullable=True),
    sa.Column('counter_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('prior_offer_count', sa.Integer(), nullable=False),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_marketplace_offers_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_marketplace_offers_inventory_item_id_inventory_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_offers')),
    sa.UniqueConstraint('account_id', 'remote_offer_id', name='offer_identity')
    )
    op.create_index(op.f('ix_marketplace_offers_account_id'), 'marketplace_offers', ['account_id'], unique=False)
    op.create_index('ix_marketplace_offers_status', 'marketplace_offers', ['status'], unique=False)
    op.create_table('marketplace_operation_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.String(length=80), nullable=False),
    sa.Column('resource_type', sa.String(length=80), nullable=True),
    sa.Column('resource_id', sa.Uuid(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.Enum('pending', 'running', 'succeeded', 'failed', 'verified', 'verification_failed', 'shadowed', name='operation_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('automation_mode', sa.String(length=50), nullable=False),
    sa.Column('request_summary', sa.JSON(), nullable=False),
    sa.Column('result_summary', sa.JSON(), nullable=False),
    sa.Column('remote_identifier', sa.String(length=200), nullable=True),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('error_category', sa.String(length=100), nullable=True),
    sa.Column('agent_identity', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_marketplace_operation_attempts_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_operation_attempts')),
    sa.UniqueConstraint('idempotency_key', name='operation_idempotency_key')
    )
    op.create_index(op.f('ix_marketplace_operation_attempts_account_id'), 'marketplace_operation_attempts', ['account_id'], unique=False)
    op.create_index('ix_operation_attempts_account', 'marketplace_operation_attempts', ['account_id', 'operation'], unique=False)
    op.create_table('marketplace_orders',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('remote_order_id', sa.String(length=200), nullable=False),
    sa.Column('remote_listing_id', sa.String(length=200), nullable=True),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.Column('buyer_reference', sa.String(length=64), nullable=True),
    sa.Column('sale_price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('shipping_charged', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('marketplace_fees', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('promotion_fees', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('payment_state', sa.String(length=50), nullable=False),
    sa.Column('status', sa.Enum('pending_payment', 'paid', 'ready_to_ship', 'shipped', 'delivered', 'cancelled', 'partially_refunded', 'refunded', 'disputed', 'unknown', name='order_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ship_by_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('shipped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refunded_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('payload_checksum', sa.String(length=128), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_marketplace_orders_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_marketplace_orders_inventory_item_id_inventory_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_orders')),
    sa.UniqueConstraint('account_id', 'remote_order_id', name='order_identity')
    )
    op.create_index(op.f('ix_marketplace_orders_account_id'), 'marketplace_orders', ['account_id'], unique=False)
    op.create_index('ix_marketplace_orders_item', 'marketplace_orders', ['inventory_item_id'], unique=False)
    op.create_index('ix_marketplace_orders_status', 'marketplace_orders', ['status'], unique=False)
    op.create_table('message_threads',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('remote_thread_id', sa.String(length=200), nullable=False),
    sa.Column('buyer_reference', sa.String(length=64), nullable=True),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.Column('last_category', sa.String(length=50), nullable=True),
    sa.Column('escalated', sa.Boolean(), nullable=False),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_message_threads_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_message_threads_inventory_item_id_inventory_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_threads')),
    sa.UniqueConstraint('account_id', 'remote_thread_id', name='thread_identity')
    )
    op.create_index(op.f('ix_message_threads_account_id'), 'message_threads', ['account_id'], unique=False)
    op.create_table('remote_listings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('remote_listing_id', sa.String(length=200), nullable=True),
    sa.Column('remote_url', sa.String(length=2000), nullable=True),
    sa.Column('marketplace_category', sa.String(length=200), nullable=True),
    sa.Column('draft_version', sa.Integer(), nullable=True),
    sa.Column('variant_version', sa.String(length=50), nullable=True),
    sa.Column('current_title', sa.String(length=500), nullable=True),
    sa.Column('description_checksum', sa.String(length=128), nullable=True),
    sa.Column('current_price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('pending_publish', 'publishing', 'active', 'reserved', 'sold', 'ending', 'ended', 'failed', 'unknown', name='remote_listing_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('sync_state', sa.Enum('clean', 'local_change_pending', 'remote_change_detected', 'conflict', 'sync_failed', name='remote_listing_sync_state', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('remote_revision', sa.String(length=100), nullable=True),
    sa.Column('last_verified_action', sa.String(length=100), nullable=True),
    sa.Column('last_error_category', sa.String(length=100), nullable=True),
    sa.Column('automation_mode_used', sa.String(length=50), nullable=True),
    sa.Column('originating_job_id', sa.Uuid(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('relist_count', sa.Integer(), nullable=False),
    sa.Column('local_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('local_version > 0', name=op.f('ck_remote_listings_local_version_positive')),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_remote_listings_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_remote_listings_inventory_item_id_inventory_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_remote_listings')),
    sa.UniqueConstraint('account_id', 'remote_listing_id', name='remote_listing_identity')
    )
    op.create_index(op.f('ix_remote_listings_account_id'), 'remote_listings', ['account_id'], unique=False)
    op.create_index(op.f('ix_remote_listings_inventory_item_id'), 'remote_listings', ['inventory_item_id'], unique=False)
    op.create_index('ix_remote_listings_item', 'remote_listings', ['inventory_item_id'], unique=False)
    op.create_index('ix_remote_listings_status', 'remote_listings', ['status'], unique=False)
    op.create_table('session_references',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('reference_key', sa.String(length=200), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=False),
    sa.Column('key_id', sa.String(length=100), nullable=False),
    sa.Column('storage_dir', sa.String(length=1000), nullable=False),
    sa.Column('allowed_domains', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_auth_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['marketplace_accounts.id'], name=op.f('fk_session_references_account_id_marketplace_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_session_references')),
    sa.UniqueConstraint('reference_key', name='session_reference_key')
    )
    op.create_index('ix_session_references_account', 'session_references', ['account_id'], unique=False)
    op.create_index(op.f('ix_session_references_account_id'), 'session_references', ['account_id'], unique=False)
    op.create_table('message_records',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('thread_id', sa.Uuid(), nullable=False),
    sa.Column('direction', sa.String(length=20), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('body_checksum', sa.String(length=128), nullable=False),
    sa.Column('delivered', sa.Boolean(), nullable=False),
    sa.Column('escalated', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['thread_id'], ['message_threads.id'], name=op.f('fk_message_records_thread_id_message_threads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_records'))
    )
    op.create_index('ix_message_records_thread', 'message_records', ['thread_id'], unique=False)
    op.create_index(op.f('ix_message_records_thread_id'), 'message_records', ['thread_id'], unique=False)
    op.create_table('reconciliation_records',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Enum('preliminary', 'final', 'discrepancy', 'resolved', name='reconciliation_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('gross_sale', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('shipping_income', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('shipping_expense', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('marketplace_fees', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('payment_fees', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('promotion_fees', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('item_cost', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('refund_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('net_proceeds', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('realized_profit', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('realized_margin', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('discrepancies', sa.JSON(), nullable=False),
    sa.Column('breakdown', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['marketplace_orders.id'], name=op.f('fk_reconciliation_records_order_id_marketplace_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliation_records'))
    )
    op.create_index('ix_reconciliation_records_order', 'reconciliation_records', ['order_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_records_order_id'), 'reconciliation_records', ['order_id'], unique=False)
    op.create_table('shipping_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('pending', 'ready', 'label_purchased', 'packed', 'shipped', 'confirmed', 'overdue', 'cancelled', name='shipping_task_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('storage_location', sa.String(length=200), nullable=True),
    sa.Column('package_profile', sa.String(length=100), nullable=True),
    sa.Column('estimated_weight_grams', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('confirmed_weight_grams', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('package_dimensions', sa.JSON(), nullable=False),
    sa.Column('ship_by_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('label_cost', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('label_reference', sa.String(length=200), nullable=True),
    sa.Column('tracking_number', sa.String(length=200), nullable=True),
    sa.Column('carrier', sa.String(length=100), nullable=True),
    sa.Column('shipped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], name=op.f('fk_shipping_tasks_inventory_item_id_inventory_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['marketplace_orders.id'], name=op.f('fk_shipping_tasks_order_id_marketplace_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shipping_tasks')),
    sa.UniqueConstraint('order_id', name='shipping_task_order')
    )
    op.create_index(op.f('ix_shipping_tasks_order_id'), 'shipping_tasks', ['order_id'], unique=False)
    op.create_index('ix_shipping_tasks_status', 'shipping_tasks', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_shipping_tasks_status', table_name='shipping_tasks')
    op.drop_index(op.f('ix_shipping_tasks_order_id'), table_name='shipping_tasks')
    op.drop_table('shipping_tasks')
    op.drop_index(op.f('ix_reconciliation_records_order_id'), table_name='reconciliation_records')
    op.drop_index('ix_reconciliation_records_order', table_name='reconciliation_records')
    op.drop_table('reconciliation_records')
    op.drop_index(op.f('ix_message_records_thread_id'), table_name='message_records')
    op.drop_index('ix_message_records_thread', table_name='message_records')
    op.drop_table('message_records')
    op.drop_index(op.f('ix_session_references_account_id'), table_name='session_references')
    op.drop_index('ix_session_references_account', table_name='session_references')
    op.drop_table('session_references')
    op.drop_index('ix_remote_listings_status', table_name='remote_listings')
    op.drop_index('ix_remote_listings_item', table_name='remote_listings')
    op.drop_index(op.f('ix_remote_listings_inventory_item_id'), table_name='remote_listings')
    op.drop_index(op.f('ix_remote_listings_account_id'), table_name='remote_listings')
    op.drop_table('remote_listings')
    op.drop_index(op.f('ix_message_threads_account_id'), table_name='message_threads')
    op.drop_table('message_threads')
    op.drop_index('ix_marketplace_orders_status', table_name='marketplace_orders')
    op.drop_index('ix_marketplace_orders_item', table_name='marketplace_orders')
    op.drop_index(op.f('ix_marketplace_orders_account_id'), table_name='marketplace_orders')
    op.drop_table('marketplace_orders')
    op.drop_index('ix_operation_attempts_account', table_name='marketplace_operation_attempts')
    op.drop_index(op.f('ix_marketplace_operation_attempts_account_id'), table_name='marketplace_operation_attempts')
    op.drop_table('marketplace_operation_attempts')
    op.drop_index('ix_marketplace_offers_status', table_name='marketplace_offers')
    op.drop_index(op.f('ix_marketplace_offers_account_id'), table_name='marketplace_offers')
    op.drop_table('marketplace_offers')
    op.drop_index('ix_inventory_reservations_item', table_name='inventory_reservations')
    op.drop_index(op.f('ix_inventory_reservations_inventory_item_id'), table_name='inventory_reservations')
    op.drop_table('inventory_reservations')
    op.drop_index('ix_webhook_events_processed', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_index('ix_sync_conflicts_status', table_name='synchronization_conflicts')
    op.drop_table('synchronization_conflicts')
    op.drop_index('ix_policy_decisions_type', table_name='policy_decisions')
    op.drop_table('policy_decisions')
    op.drop_index('ix_marketplace_accounts_status', table_name='marketplace_accounts')
    op.drop_table('marketplace_accounts')
    op.drop_index('ix_exception_tasks_status', table_name='exception_tasks')
    op.drop_table('exception_tasks')
    op.drop_table('emergency_stop_state')
    op.drop_index('ix_circuit_breakers_state', table_name='circuit_breakers')
    op.drop_table('circuit_breakers')
    op.drop_table('automation_policy_versions')
