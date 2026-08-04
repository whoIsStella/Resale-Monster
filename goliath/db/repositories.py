from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from goliath.db.models import (
    HUMAN_ONLY_INVENTORY_STATUSES,
    AgentJobRecord,
    AgentJobStatus,
    ApprovalAction,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    InventoryCondition,
    InventoryItem,
    InventoryStatus,
    ListingStatus,
    MarketplaceListing,
    utc_now,
)


class RecordNotFoundError(LookupError):
    pass


class InvalidStateTransitionError(ValueError):
    pass


class VersionConflictError(ValueError):
    """Raised when an optimistic-concurrency version check fails."""


class ForbiddenStatusError(ValueError):
    """Raised when an agent-safe update targets a human-only inventory status."""


# Draft fields an agent-safe inventory update is permitted to write.
AGENT_WRITABLE_INVENTORY_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "brand",
        "model_name",
        "category",
        "subcategory",
        "department",
        "size_label",
        "normalized_size",
        "colors",
        "materials",
        "pattern",
        "condition_grade",
        "condition_notes",
        "defects",
        "acquisition_source",
        "storage_location",
        "estimated_weight_grams",
        "packed_weight_grams",
        "package_dimensions",
        "attributes",
        "research_confidence",
        "identification_confidence",
        "status",
    }
)


class InventoryRepository:
    """Bounded inventory operations; deliberately exposes no raw SQL interface."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        sku: str,
        title: str,
        condition: InventoryCondition,
        acquisition_cost: Decimal,
        description: str | None = None,
        quantity: int = 1,
        currency: str = "USD",
        attributes: dict[str, Any] | None = None,
        status: InventoryStatus = InventoryStatus.DRAFT,
        **fields: Any,
    ) -> InventoryItem:
        if quantity < 0:
            raise ValueError("quantity must be nonnegative")
        if acquisition_cost < 0:
            raise ValueError("acquisition_cost must be nonnegative")
        allowed = {
            column.name for column in InventoryItem.__table__.columns
        } - {"id", "sku", "title", "condition", "acquisition_cost", "created_at", "updated_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown inventory fields: {', '.join(sorted(unknown))}")
        item = InventoryItem(
            sku=sku,
            title=title,
            description=description,
            condition=condition,
            quantity=quantity,
            acquisition_cost=acquisition_cost,
            currency=_currency(currency),
            attributes=attributes or {},
            status=status,
            **fields,
        )
        self._session.add(item)
        self._session.flush()
        return item

    def get(self, item_id: UUID) -> InventoryItem | None:
        return self._session.get(InventoryItem, item_id)

    def get_by_sku(self, sku: str) -> InventoryItem | None:
        return self._session.scalar(select(InventoryItem).where(InventoryItem.sku == sku))

    def list(
        self,
        *,
        status: InventoryStatus | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[InventoryItem]:
        statement = select(InventoryItem)
        if status is not None:
            statement = statement.where(InventoryItem.status == status)
        if category is not None:
            statement = statement.where(InventoryItem.category == category)
        statement = statement.order_by(InventoryItem.created_at).limit(limit).offset(offset)
        return self._session.scalars(statement).all()

    def search(self, query: str, *, limit: int = 50) -> Sequence[InventoryItem]:
        pattern = f"%{query.lower()}%"
        statement = (
            select(InventoryItem)
            .where(
                func.lower(InventoryItem.title).like(pattern)
                | func.lower(InventoryItem.sku).like(pattern)
                | func.lower(func.coalesce(InventoryItem.brand, "")).like(pattern)
            )
            .order_by(InventoryItem.created_at)
            .limit(limit)
        )
        return self._session.scalars(statement).all()

    def set_quantity(self, item_id: UUID, quantity: int) -> InventoryItem:
        if quantity < 0:
            raise ValueError("quantity must be nonnegative")
        item = self.get(item_id)
        if item is None:
            raise RecordNotFoundError(f"inventory item not found: {item_id}")
        item.quantity = quantity
        item.updated_at = utc_now()
        self._session.flush()
        return item

    def update_draft(
        self,
        item_id: UUID,
        *,
        expected_version: int,
        changes: dict[str, Any],
        allow_human_only_status: bool = False,
    ) -> InventoryItem:
        """Apply an optimistic, agent-safe draft update using a versioned compare-and-swap."""
        unknown = set(changes) - AGENT_WRITABLE_INVENTORY_FIELDS
        if unknown:
            raise ValueError(f"fields are not draft-writable: {', '.join(sorted(unknown))}")
        if "status" in changes:
            target = InventoryStatus(changes["status"])
            if target in HUMAN_ONLY_INVENTORY_STATUSES and not allow_human_only_status:
                raise ForbiddenStatusError(
                    f"status {target.value} requires human authorization or a deterministic service"
                )
            changes = {**changes, "status": target}
        item = self.get(item_id)
        if item is None:
            raise RecordNotFoundError(f"inventory item not found: {item_id}")
        values = {**changes, "version": expected_version + 1, "updated_at": utc_now()}
        result = self._session.execute(
            update(InventoryItem)
            .where(
                InventoryItem.id == item_id,
                InventoryItem.version == expected_version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise VersionConflictError(
                f"stale inventory version: expected {expected_version} for {item_id}"
            )
        self._session.expire(item)
        return item


class MarketplaceListingRepository:
    """Draft-only listing storage; live marketplace mutations are intentionally absent."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_draft(
        self,
        *,
        inventory_item_id: UUID,
        marketplace: str,
        title: str,
        price: Decimal,
        description: str | None = None,
        currency: str = "USD",
        quantity: int = 1,
        listing_data: dict[str, Any] | None = None,
    ) -> MarketplaceListing:
        if self._session.get(InventoryItem, inventory_item_id) is None:
            raise RecordNotFoundError(f"inventory item not found: {inventory_item_id}")
        if price < 0:
            raise ValueError("price must be nonnegative")
        if quantity < 0:
            raise ValueError("quantity must be nonnegative")
        listing = MarketplaceListing(
            inventory_item_id=inventory_item_id,
            marketplace=marketplace,
            status=ListingStatus.DRAFT,
            title=title,
            description=description,
            price=price,
            currency=_currency(currency),
            quantity=quantity,
            listing_data=listing_data or {},
        )
        self._session.add(listing)
        self._session.flush()
        return listing

    def get(self, listing_id: UUID) -> MarketplaceListing | None:
        return self._session.get(MarketplaceListing, listing_id)

    def list_for_inventory_item(self, item_id: UUID) -> Sequence[MarketplaceListing]:
        statement = (
            select(MarketplaceListing)
            .where(MarketplaceListing.inventory_item_id == item_id)
            .order_by(MarketplaceListing.created_at)
        )
        return self._session.scalars(statement).all()

    def submit_for_approval(self, listing_id: UUID) -> MarketplaceListing:
        listing = self.get(listing_id)
        if listing is None:
            raise RecordNotFoundError(f"marketplace listing not found: {listing_id}")
        if listing.status is not ListingStatus.DRAFT:
            raise InvalidStateTransitionError("only draft listings can be submitted for approval")
        listing.status = ListingStatus.PENDING_APPROVAL
        listing.updated_at = utc_now()
        self._session.flush()
        return listing


class AgentJobRepository:
    VALID_TRANSITIONS: ClassVar[dict[AgentJobStatus, frozenset[AgentJobStatus]]] = {
        AgentJobStatus.PENDING: frozenset(
            {
                AgentJobStatus.QUEUED,
                AgentJobStatus.FAILED,
                AgentJobStatus.CANCELLED,
            }
        ),
        AgentJobStatus.QUEUED: frozenset(
            {
                AgentJobStatus.RUNNING,
                AgentJobStatus.FAILED,
                AgentJobStatus.CANCELLED,
            }
        ),
        AgentJobStatus.RUNNING: frozenset(
            {
                AgentJobStatus.QUEUED,
                AgentJobStatus.SUCCEEDED,
                AgentJobStatus.FAILED,
                AgentJobStatus.TIMED_OUT,
                AgentJobStatus.CANCELLED,
            }
        ),
        AgentJobStatus.SUCCEEDED: frozenset(),
        AgentJobStatus.FAILED: frozenset(),
        AgentJobStatus.TIMED_OUT: frozenset(),
        AgentJobStatus.CANCELLED: frozenset(),
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        job_id: UUID,
        agent_name: str | None = None,
        requested_agent: str | None = None,
        task_type: str,
        objective: str,
        risk_tier: str,
        workspace_path: str = ".",
        permissions: list[str] | None = None,
        context_files: list[str] | None = None,
        forbidden_actions: list[str] | None = None,
        timeout_seconds: int = 600,
        max_output_chars: int = 100_000,
        job_metadata: dict[str, Any] | None = None,
    ) -> AgentJobRecord:
        record = AgentJobRecord(
            id=job_id,
            requested_agent=requested_agent,
            agent_name=agent_name,
            task_type=task_type,
            objective=objective,
            workspace_path=workspace_path,
            permissions=permissions or [],
            context_files=context_files or [],
            forbidden_actions=forbidden_actions or [],
            risk_tier=risk_tier,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            job_metadata=job_metadata or {},
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get(self, job_id: UUID) -> AgentJobRecord | None:
        return self._session.get(AgentJobRecord, job_id)

    def list(
        self,
        *,
        status: AgentJobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AgentJobRecord]:
        statement = select(AgentJobRecord)
        if status is not None:
            statement = statement.where(AgentJobRecord.status == status)
        statement = statement.order_by(AgentJobRecord.created_at).limit(limit).offset(offset)
        return self._session.scalars(statement).all()

    def transition(
        self,
        job_id: UUID,
        to_status: AgentJobStatus,
        *,
        expected_version: int,
        agent_name: str | None = None,
        failure_reason: str | None = None,
        cancellation_reason: str | None = None,
        now: datetime | None = None,
    ) -> AgentJobRecord:
        record = self.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        from_status = record.status
        if to_status not in self.VALID_TRANSITIONS[from_status]:
            raise InvalidStateTransitionError(
                f"invalid agent job transition: {from_status.value} -> {to_status.value}"
            )
        timestamp = now or utc_now()
        values: dict[str, Any] = {
            "status": to_status,
            "version": expected_version + 1,
        }
        if agent_name is not None:
            values["agent_name"] = agent_name
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        if to_status is AgentJobStatus.QUEUED:
            values["queued_at"] = timestamp
        elif to_status is AgentJobStatus.RUNNING:
            values["started_at"] = timestamp
        elif to_status is AgentJobStatus.CANCELLED:
            values.update(
                cancelled_at=timestamp,
                completed_at=timestamp,
                cancellation_reason=cancellation_reason,
            )
        elif to_status in {
            AgentJobStatus.SUCCEEDED,
            AgentJobStatus.FAILED,
            AgentJobStatus.TIMED_OUT,
        }:
            values["completed_at"] = timestamp

        result = self._session.execute(
            update(AgentJobRecord)
            .where(
                AgentJobRecord.id == job_id,
                AgentJobRecord.version == expected_version,
                AgentJobRecord.status == from_status,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise InvalidStateTransitionError(
                f"stale agent job version: expected {expected_version} for {job_id}"
            )
        self._session.expire(record)
        return record

    def claim_next(self) -> AgentJobRecord | None:
        candidates = self._session.scalars(
            select(AgentJobRecord)
            .where(AgentJobRecord.status == AgentJobStatus.QUEUED)
            .order_by(AgentJobRecord.queued_at, AgentJobRecord.created_at)
            .limit(20)
        ).all()
        for record in candidates:
            try:
                return self.transition(
                    record.id,
                    AgentJobStatus.RUNNING,
                    expected_version=record.version,
                )
            except InvalidStateTransitionError:
                continue
        return None

    def store_execution_result(
        self,
        job_id: UUID,
        *,
        process_id: int | None,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        structured_result: dict[str, Any] | None,
        failure_reason: str | None,
        timed_out: bool,
        output_truncated: bool,
        stdout_total_chars: int,
        stderr_total_chars: int,
    ) -> AgentJobRecord:
        record = self.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        record.process_id = process_id
        record.exit_code = exit_code
        record.stdout = stdout
        record.stderr = stderr
        record.structured_result = structured_result
        record.failure_reason = failure_reason
        record.timed_out = timed_out
        record.output_truncated = output_truncated
        record.stdout_total_chars = stdout_total_chars
        record.stderr_total_chars = stderr_total_chars
        self._session.flush()
        return record

    def set_process_id(self, job_id: UUID, process_id: int) -> AgentJobRecord:
        record = self.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        if record.status is not AgentJobStatus.RUNNING:
            raise InvalidStateTransitionError("process ID can only be set on a running job")
        record.process_id = process_id
        self._session.flush()
        return record

    def mark_running(self, job_id: UUID, *, started_at: datetime | None = None) -> AgentJobRecord:
        record = self.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        return self.transition(
            job_id,
            AgentJobStatus.RUNNING,
            expected_version=record.version,
            now=started_at,
        )

    def complete(
        self,
        job_id: UUID,
        *,
        exit_code: int,
        timed_out: bool = False,
        completed_at: datetime | None = None,
    ) -> AgentJobRecord:
        record = self.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        record.exit_code = exit_code
        record.timed_out = timed_out
        target = (
            AgentJobStatus.TIMED_OUT
            if timed_out
            else AgentJobStatus.SUCCEEDED
            if exit_code == 0
            else AgentJobStatus.FAILED
        )
        return self.transition(
            job_id,
            target,
            expected_version=record.version,
            now=completed_at,
        )


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def request(
        self,
        *,
        action: ApprovalAction,
        resource_type: str,
        resource_id: UUID,
        requested_by: str,
        requested_changes: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            requested_by=requested_by,
            requested_changes=requested_changes or {},
        )
        self._session.add(approval)
        self._session.flush()
        return approval

    def get(self, approval_id: UUID) -> ApprovalRequest | None:
        return self._session.get(ApprovalRequest, approval_id)

    def list_pending(self) -> Sequence[ApprovalRequest]:
        statement = (
            select(ApprovalRequest)
            .where(ApprovalRequest.status == ApprovalStatus.PENDING)
            .order_by(ApprovalRequest.created_at)
        )
        return self._session.scalars(statement).all()

    def decide(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        reviewed_by: str,
        review_note: str | None = None,
    ) -> ApprovalRequest:
        approval = self.get(approval_id)
        if approval is None:
            raise RecordNotFoundError(f"approval request not found: {approval_id}")
        if approval.status is not ApprovalStatus.PENDING:
            raise InvalidStateTransitionError("approval request has already been decided")
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.reviewed_by = reviewed_by
        approval.review_note = review_note
        approval.reviewed_at = utc_now()
        self._session.flush()
        return approval


class AuditEventRepository:
    """Append-only audit access; update and delete operations are intentionally absent."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        resource_type: str,
        resource_id: UUID,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
        self._session.add(event)
        self._session.flush()
        return event

    def list_for_resource(
        self, resource_type: str, resource_id: UUID, *, limit: int = 100
    ) -> Sequence[AuditEvent]:
        statement = (
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == resource_type,
                AuditEvent.resource_id == resource_id,
            )
            .order_by(AuditEvent.created_at)
            .limit(limit)
        )
        return self._session.scalars(statement).all()


def _currency(value: str) -> str:
    normalized = value.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("currency must be a three-letter code")
    return normalized
