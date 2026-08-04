"""Create milestone one persistence tables.

Revision ID: 20260802_0001
Revises:
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


inventory_condition = sa.Enum(
    "new",
    "like_new",
    "good",
    "fair",
    "poor",
    name="inventory_condition",
    native_enum=False,
    create_constraint=True,
)
listing_status = sa.Enum(
    "draft",
    "pending_approval",
    "ready",
    "published",
    "ended",
    "error",
    name="listing_status",
    native_enum=False,
    create_constraint=True,
)
agent_job_status = sa.Enum(
    "queued",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    name="agent_job_status",
    native_enum=False,
    create_constraint=True,
)
approval_action = sa.Enum(
    "publish_listing",
    "change_price",
    "accept_offer",
    "issue_refund",
    "contact_buyer",
    name="approval_action",
    native_enum=False,
    create_constraint=True,
)
approval_status = sa.Enum(
    "pending",
    "approved",
    "rejected",
    "cancelled",
    name="approval_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "agent_job_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(length=100), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("risk_tier", sa.String(length=50), nullable=False),
        sa.Column("status", agent_job_status, nullable=False),
        sa.Column("job_metadata", sa.JSON(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_job_records")),
    )
    op.create_index(op.f("ix_agent_job_records_agent_name"), "agent_job_records", ["agent_name"])
    op.create_index(op.f("ix_agent_job_records_status"), "agent_job_records", ["status"])

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", approval_action, nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("requested_changes", sa.JSON(), nullable=False),
        sa.Column("status", approval_status, nullable=False),
        sa.Column("requested_by", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=200), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
    )
    op.create_index(
        "ix_approval_requests_status_created", "approval_requests", ["status", "created_at"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"])
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id", "created_at"],
    )

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("condition", inventory_condition, nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("acquisition_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "acquisition_cost >= 0", name=op.f("ck_inventory_items_acquisition_cost_nonnegative")
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_inventory_items_quantity_nonnegative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_items")),
        sa.UniqueConstraint("sku", name=op.f("uq_inventory_items_sku")),
    )

    op.create_table(
        "marketplace_listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inventory_item_id", sa.Uuid(), nullable=False),
        sa.Column("marketplace", sa.String(length=50), nullable=False),
        sa.Column("external_listing_id", sa.String(length=200), nullable=True),
        sa.Column("status", listing_status, nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("listing_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price >= 0", name=op.f("ck_marketplace_listings_price_nonnegative")),
        sa.CheckConstraint(
            "quantity >= 0", name=op.f("ck_marketplace_listings_quantity_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["inventory_item_id"],
            ["inventory_items.id"],
            name=op.f("fk_marketplace_listings_inventory_item_id_inventory_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marketplace_listings")),
        sa.UniqueConstraint(
            "marketplace", "external_listing_id", name=op.f("uq_marketplace_listings_marketplace")
        ),
    )
    op.create_index(
        op.f("ix_marketplace_listings_inventory_item_id"),
        "marketplace_listings",
        ["inventory_item_id"],
    )
    op.create_index(
        "ix_marketplace_listings_inventory_status",
        "marketplace_listings",
        ["inventory_item_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_listings_inventory_status", table_name="marketplace_listings")
    op.drop_index(
        op.f("ix_marketplace_listings_inventory_item_id"), table_name="marketplace_listings"
    )
    op.drop_table("marketplace_listings")
    op.drop_table("inventory_items")
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_approval_requests_status_created", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index(op.f("ix_agent_job_records_status"), table_name="agent_job_records")
    op.drop_index(op.f("ix_agent_job_records_agent_name"), table_name="agent_job_records")
    op.drop_table("agent_job_records")
