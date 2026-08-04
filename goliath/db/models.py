from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from goliath.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_type(enum_class: type[StrEnum], name: str) -> Enum:
    """Store stable enum values, with portable constraints on every database."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class InventoryCondition(StrEnum):
    NEW = "new"
    LIKE_NEW = "like_new"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class InventoryStatus(StrEnum):
    DRAFT = "draft"
    RESEARCH_NEEDED = "research_needed"
    READY_FOR_LISTING = "ready_for_listing"
    LISTED = "listed"
    RESERVED = "reserved"
    SOLD = "sold"
    ARCHIVED = "archived"
    DONATED = "donated"
    LOST = "lost"


# Statuses an agent-safe draft update may never move an item into directly.
HUMAN_ONLY_INVENTORY_STATUSES: frozenset[InventoryStatus] = frozenset(
    {
        InventoryStatus.LISTED,
        InventoryStatus.SOLD,
        InventoryStatus.DONATED,
        InventoryStatus.ARCHIVED,
    }
)


class MediaRole(StrEnum):
    ORIGINAL = "original"
    PROCESSED = "processed"
    LABEL = "label"
    DEFECT = "defect"
    MEASUREMENT = "measurement"
    RECEIPT = "receipt"
    DOCUMENT = "document"


class MediaProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"


class MeasurementUnit(StrEnum):
    CM = "cm"
    IN = "in"


class ResearchStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    REJECTED = "rejected"


class SourceReliability(StrEnum):
    OFFICIAL = "official"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Marketplace(StrEnum):
    EBAY = "ebay"
    POSHMARK = "poshmark"
    DEPOP = "depop"
    MERCARI = "mercari"
    GRAILED = "grailed"
    FACEBOOK = "facebook"
    GENERIC = "generic"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ProposalType(StrEnum):
    INVENTORY_UPDATE = "inventory_update"
    IDENTIFICATION_SELECTION = "identification_selection"
    PRICING_CHANGE = "pricing_change"
    LISTING_DRAFT_APPROVAL = "listing_draft_approval"
    LISTING_VARIANT_APPROVAL = "listing_variant_approval"
    ARCHIVE_RECOMMENDATION = "archive_recommendation"
    RESEARCH_RESOLUTION = "research_resolution"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"


class ListingStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    PUBLISHED = "published"
    ENDED = "ended"
    ERROR = "error"


class AgentJobStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ApprovalAction(StrEnum):
    PUBLISH_LISTING = "publish_listing"
    CHANGE_PRICE = "change_price"
    ACCEPT_OFFER = "accept_offer"
    ISSUE_REFUND = "issue_refund"
    CONTACT_BUYER = "contact_buyer"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        CheckConstraint("acquisition_cost >= 0", name="acquisition_cost_nonnegative"),
        CheckConstraint("cost_basis is null or cost_basis >= 0", name="cost_basis_nonnegative"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_inventory_items_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    condition: Mapped[InventoryCondition] = mapped_column(
        enum_type(InventoryCondition, "inventory_condition"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acquisition_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Milestone four: typed resale-domain fields.
    brand: Mapped[str | None] = mapped_column(String(200))
    model_name: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    subcategory: Mapped[str | None] = mapped_column(String(100))
    department: Mapped[str | None] = mapped_column(String(50))
    size_label: Mapped[str | None] = mapped_column(String(50))
    normalized_size: Mapped[str | None] = mapped_column(String(50))
    colors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    materials: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pattern: Mapped[str | None] = mapped_column(String(100))
    condition_grade: Mapped[str | None] = mapped_column(String(50))
    condition_notes: Mapped[str | None] = mapped_column(Text)
    defects: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    acquisition_date: Mapped[date | None] = mapped_column(Date)
    acquisition_source: Mapped[str | None] = mapped_column(String(200))
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    estimated_weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    packed_weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    package_dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    storage_location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[InventoryStatus] = mapped_column(
        enum_type(InventoryStatus, "inventory_status"),
        nullable=False,
        default=InventoryStatus.DRAFT,
    )
    research_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    identification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    listings: Mapped[list[MarketplaceListing]] = relationship(back_populates="inventory_item")


class MarketplaceListing(Base):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_nonnegative"),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        UniqueConstraint("marketplace", "external_listing_id"),
        Index("ix_marketplace_listings_inventory_status", "inventory_item_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    marketplace: Mapped[str] = mapped_column(String(50), nullable=False)
    external_listing_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ListingStatus] = mapped_column(
        enum_type(ListingStatus, "listing_status"), nullable=False, default=ListingStatus.DRAFT
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    listing_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    inventory_item: Mapped[InventoryItem] = relationship(back_populates="listings")


class AgentJobRecord(Base):
    __tablename__ = "agent_job_records"
    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        CheckConstraint("max_output_chars > 0", name="max_output_chars_positive"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_agent_job_records_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    requested_agent: Mapped[str | None] = mapped_column(String(100))
    agent_name: Mapped[str | None] = mapped_column(String(100), index=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    forbidden_actions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[AgentJobStatus] = mapped_column(
        enum_type(AgentJobStatus, "agent_job_status"),
        nullable=False,
        default=AgentJobStatus.PENDING,
        index=True,
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(nullable=False, default=False)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    output_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    stdout_total_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stderr_total_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    process_id: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_reason: Mapped[str | None] = mapped_column(Text)
    prior_worker_id: Mapped[str | None] = mapped_column(String(200))
    prior_lease_identifier: Mapped[str | None] = mapped_column(String(128))
    recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_status_created", "status", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action: Mapped[ApprovalAction] = mapped_column(
        enum_type(ApprovalAction, "approval_action"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    requested_changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_type(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    requested_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_resource", "resource_type", "resource_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class WorkerStatus(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DRAINING = "draining"
    STOPPED = "stopped"
    STALE = "stale"


class WorkerRecord(Base):
    __tablename__ = "worker_records"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    status: Mapped[WorkerStatus] = mapped_column(
        enum_type(WorkerStatus, "worker_status"), nullable=False
    )
    configured_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    active_job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    software_version: Mapped[str] = mapped_column(String(50), nullable=False)
    shutdown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobLease(Base):
    __tablename__ = "job_leases"

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_job_records.id", ondelete="CASCADE"), primary_key=True
    )
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("worker_records.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    lease_identifier: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    lease_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_job_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    failure_category: Mapped[str | None] = mapped_column(String(100))


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    objective_template: Mapped[str] = mapped_column(Text, nullable=False)
    requested_agent: Mapped[str | None] = mapped_column(String(100))
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    timing_type: Mapped[str] = mapped_column(String(20), nullable=False)
    timing_value: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC")
    missed_run_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="skip")
    max_catch_up_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ApiPrincipal(Base):
    __tablename__ = "api_principals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_principals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("principal_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_principals.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


# --------------------------------------------------------------------------- #
# Milestone four: resale-domain tables
# --------------------------------------------------------------------------- #


class InventoryMedia(Base):
    """Metadata for inventory files; the bytes live on disk/object storage, never here."""

    __tablename__ = "inventory_media"
    __table_args__ = (
        CheckConstraint("file_size >= 0", name="file_size_nonnegative"),
        UniqueConstraint("inventory_item_id", "checksum", name="inventory_item_checksum"),
        Index("ix_inventory_media_item", "inventory_item_id", "role"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[MediaRole] = mapped_column(enum_type(MediaRole, "media_role"), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    processing_status: Mapped[MediaProcessingStatus] = mapped_column(
        enum_type(MediaProcessingStatus, "media_processing_status"),
        nullable=False,
        default=MediaProcessingStatus.PENDING,
    )
    processing_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint("value_cm > 0", name="value_cm_positive"),
        CheckConstraint(
            "confidence is null or (confidence >= 0 and confidence <= 1)",
            name="confidence_range",
        ),
        Index("ix_measurements_item", "inventory_item_id", "measurement_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    measurement_type: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    unit: Mapped[MeasurementUnit] = mapped_column(
        enum_type(MeasurementUnit, "measurement_unit"), nullable=False
    )
    value_cm: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    method: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    source: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ResearchRecord(Base):
    __tablename__ = "research_records"
    __table_args__ = (
        CheckConstraint(
            "confidence is null or (confidence >= 0 and confidence <= 1)",
            name="confidence_range",
        ),
        Index("ix_research_records_item_status", "inventory_item_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    researcher: Mapped[str] = mapped_column(String(200), nullable=False)
    search_terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    observations: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    selected_identification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    unresolved_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[ResearchStatus] = mapped_column(
        enum_type(ResearchStatus, "research_status"),
        nullable=False,
        default=ResearchStatus.PENDING,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchSource(Base):
    __tablename__ = "research_sources"
    __table_args__ = (
        CheckConstraint(
            "relevance_score is null or (relevance_score >= 0 and relevance_score <= 1)",
            name="relevance_range",
        ),
        UniqueConstraint("research_id", "content_hash", name="research_content_hash"),
        Index("ix_research_sources_research", "research_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    research_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(2000))
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    excerpt: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reliability: Mapped[SourceReliability] = mapped_column(
        enum_type(SourceReliability, "source_reliability"),
        nullable=False,
        default=SourceReliability.UNKNOWN,
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class IdentificationCandidate(Base):
    __tablename__ = "identification_candidates"
    __table_args__ = (
        CheckConstraint(
            "confidence is null or (confidence >= 0 and confidence <= 1)",
            name="confidence_range",
        ),
        Index("ix_identification_candidates_research", "research_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    research_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand: Mapped[str | None] = mapped_column(String(200))
    model_name: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    evidence_source_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ComparableSale(Base):
    """Immutable comparable-sale evidence; invalidation is the only permitted mutation."""

    __tablename__ = "comparable_sales"
    __table_args__ = (
        UniqueConstraint("inventory_item_id", "marketplace", "source_identity", name="comp_identity"),
        Index("ix_comparable_sales_item", "inventory_item_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marketplace: Mapped[Marketplace] = mapped_column(
        enum_type(Marketplace, "comparable_marketplace"), nullable=False
    )
    source_identity: Mapped[str] = mapped_column(String(300), nullable=False)
    listing_title: Mapped[str] = mapped_column(String(500), nullable=False)
    is_sold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    listed_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sold_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    shipping_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    condition: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[str | None] = mapped_column(String(50))
    sale_date: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    similarity_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reliability_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    notes: Mapped[str | None] = mapped_column(Text)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PricingRecommendation(Base):
    __tablename__ = "pricing_recommendations"
    __table_args__ = (Index("ix_pricing_recommendations_item", "inventory_item_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    fee_version: Mapped[str] = mapped_column(String(50), nullable=False)
    recommended_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    fast_sale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    minimum_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_net_proceeds: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_profit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_margin: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class MasterListingDraft(Base):
    __tablename__ = "master_listing_drafts"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_master_listing_drafts_item_status", "inventory_item_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    subcategory: Mapped[str | None] = mapped_column(String(100))
    condition: Mapped[str | None] = mapped_column(String(50))
    condition_details: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(String(50))
    colors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    materials: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    style_keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    measurements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    defects: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pricing_recommendation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pricing_recommendations.id", ondelete="SET NULL")
    )
    image_order: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    shipping_assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[DraftStatus] = mapped_column(
        enum_type(DraftStatus, "master_draft_status"),
        nullable=False,
        default=DraftStatus.DRAFT,
    )
    validation_warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ListingDraftVersion(Base):
    """Append-only history so approved draft content is never silently overwritten."""

    __tablename__ = "listing_draft_versions"
    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="draft_version"),
        Index("ix_listing_draft_versions_draft", "draft_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("master_listing_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DraftStatus] = mapped_column(
        enum_type(DraftStatus, "draft_version_status"), nullable=False
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class MarketplaceDraftVariant(Base):
    __tablename__ = "marketplace_draft_variants"
    __table_args__ = (
        UniqueConstraint("draft_id", "marketplace", name="variant_marketplace"),
        Index("ix_marketplace_draft_variants_draft", "draft_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("master_listing_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marketplace: Mapped[Marketplace] = mapped_column(
        enum_type(Marketplace, "variant_marketplace"), nullable=False
    )
    constraint_version: Mapped[str] = mapped_column(String(50), nullable=False)
    marketplace_title: Mapped[str] = mapped_column(String(500), nullable=False)
    marketplace_description: Mapped[str | None] = mapped_column(Text)
    category_mapping: Mapped[str | None] = mapped_column(String(200))
    item_specifics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    hashtags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    proposed_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    shipping_profile: Mapped[str | None] = mapped_column(String(200))
    image_order: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validation_warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[DraftStatus] = mapped_column(
        enum_type(DraftStatus, "variant_status"),
        nullable=False,
        default=DraftStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class DomainProposal(Base):
    __tablename__ = "domain_proposals"
    __table_args__ = (Index("ix_domain_proposals_status_type", "status", "proposal_type"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    proposal_type: Mapped[ProposalType] = mapped_column(
        enum_type(ProposalType, "proposal_type"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ProposalStatus] = mapped_column(
        enum_type(ProposalStatus, "proposal_status"),
        nullable=False,
        default=ProposalStatus.PENDING,
    )
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    execution_error: Mapped[str | None] = mapped_column(Text)


class ProposalEvidence(Base):
    __tablename__ = "proposal_evidence"
    __table_args__ = (Index("ix_proposal_evidence_proposal", "proposal_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("domain_proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_id: Mapped[UUID | None] = mapped_column(Uuid)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CategoryCompletenessRule(Base):
    __tablename__ = "category_completeness_rules"
    __table_args__ = (UniqueConstraint("category", "version", name="category_version"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    required_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recommended_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_measurements: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class MarketplaceConstraintVersion(Base):
    __tablename__ = "marketplace_constraint_versions"
    __table_args__ = (
        UniqueConstraint("marketplace", "version", name="marketplace_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    marketplace: Mapped[Marketplace] = mapped_column(
        enum_type(Marketplace, "constraint_marketplace"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    max_title_length: Mapped[int] = mapped_column(Integer, nullable=False)
    required_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_conditions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    category_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class McpServicePrincipal(Base):
    """MCP service principals are stored with only a hashed credential, separate from API keys."""

    __tablename__ = "mcp_service_principals"
    __table_args__ = (UniqueConstraint("credential_hash", name="mcp_credential_hash"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    credential_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
