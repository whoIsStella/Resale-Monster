from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from goliath.db.models import (
    AgentJobStatus,
    ApprovalAction,
    ApprovalStatus,
    InventoryCondition,
    ListingStatus,
)
from goliath.db.repositories import (
    AgentJobRepository,
    ApprovalRepository,
    AuditEventRepository,
    InvalidStateTransitionError,
    InventoryRepository,
    MarketplaceListingRepository,
    RecordNotFoundError,
)


def create_inventory(session: Session, *, sku: str = "SKU-1"):
    return InventoryRepository(session).create(
        sku=sku,
        title="Vintage jacket",
        description="Wool jacket",
        condition=InventoryCondition.GOOD,
        quantity=1,
        acquisition_cost=Decimal("24.50"),
        attributes={"size": "M"},
    )


def test_inventory_repository_creates_reads_lists_and_updates(session: Session) -> None:
    repository = InventoryRepository(session)
    item = create_inventory(session)

    assert repository.get(item.id) is item
    assert repository.get_by_sku("SKU-1") is item
    assert repository.list() == [item]
    assert item.currency == "USD"
    assert item.attributes == {"size": "M"}

    updated = repository.set_quantity(item.id, 3)
    assert updated.quantity == 3


@pytest.mark.parametrize(
    ("quantity", "cost", "message"),
    [(-1, Decimal(1), "quantity"), (1, Decimal(-1), "acquisition_cost")],
)
def test_inventory_repository_rejects_negative_values(
    session: Session, quantity: int, cost: Decimal, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        InventoryRepository(session).create(
            sku="BAD",
            title="Bad item",
            condition=InventoryCondition.FAIR,
            quantity=quantity,
            acquisition_cost=cost,
        )


def test_inventory_sku_is_unique(session: Session) -> None:
    create_inventory(session)
    with pytest.raises(IntegrityError):
        create_inventory(session)


def test_inventory_update_requires_existing_item(session: Session) -> None:
    with pytest.raises(RecordNotFoundError):
        InventoryRepository(session).set_quantity(uuid4(), 1)


def test_listing_repository_is_draft_only_and_requires_inventory(session: Session) -> None:
    repository = MarketplaceListingRepository(session)
    item = create_inventory(session)
    listing = repository.create_draft(
        inventory_item_id=item.id,
        marketplace="example-market",
        title="Vintage wool jacket",
        price=Decimal("79.99"),
        listing_data={"category": "outerwear"},
    )

    assert listing.status is ListingStatus.DRAFT
    assert listing.external_listing_id is None
    assert repository.get(listing.id) is listing
    assert repository.list_for_inventory_item(item.id) == [listing]
    assert not hasattr(repository, "publish")
    assert not hasattr(repository, "change_price")

    repository.submit_for_approval(listing.id)
    assert listing.status is ListingStatus.PENDING_APPROVAL
    with pytest.raises(InvalidStateTransitionError):
        repository.submit_for_approval(listing.id)


def test_listing_repository_validates_input(session: Session) -> None:
    repository = MarketplaceListingRepository(session)
    with pytest.raises(RecordNotFoundError):
        repository.create_draft(
            inventory_item_id=uuid4(), marketplace="example", title="Missing", price=Decimal(1)
        )
    item = create_inventory(session)
    with pytest.raises(ValueError, match="price"):
        repository.create_draft(
            inventory_item_id=item.id,
            marketplace="example",
            title="Negative",
            price=Decimal(-1),
        )


def test_agent_job_repository_enforces_lifecycle(session: Session) -> None:
    repository = AgentJobRepository(session)
    job_id = uuid4()
    record = repository.create(
        job_id=job_id,
        agent_name="codex",
        task_type="code_change",
        objective="Add persistence",
        risk_tier="draft",
        job_metadata={"source": "test"},
    )
    assert record.status is AgentJobStatus.PENDING
    record = repository.transition(
        job_id,
        AgentJobStatus.QUEUED,
        expected_version=record.version,
        agent_name="codex",
    )

    repository.mark_running(job_id)
    assert record.status is AgentJobStatus.RUNNING
    assert record.started_at is not None

    repository.complete(job_id, exit_code=0)
    assert record.status is AgentJobStatus.SUCCEEDED
    assert record.completed_at is not None
    with pytest.raises(InvalidStateTransitionError):
        repository.complete(job_id, exit_code=0)


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [(1, False, AgentJobStatus.FAILED), (124, True, AgentJobStatus.TIMED_OUT)],
)
def test_agent_job_terminal_statuses(
    session: Session, exit_code: int, timed_out: bool, expected: AgentJobStatus
) -> None:
    repository = AgentJobRepository(session)
    job_id = uuid4()
    record = repository.create(
        job_id=job_id,
        agent_name="test",
        task_type="test",
        objective="Test status",
        risk_tier="read_only",
    )
    repository.transition(
        job_id,
        AgentJobStatus.QUEUED,
        expected_version=record.version,
        agent_name="test",
    )
    repository.mark_running(job_id)
    assert repository.complete(job_id, exit_code=exit_code, timed_out=timed_out).status is expected


def test_approval_repository_requires_one_human_decision(session: Session) -> None:
    repository = ApprovalRepository(session)
    resource_id = uuid4()
    approval = repository.request(
        action=ApprovalAction.PUBLISH_LISTING,
        resource_type="marketplace_listing",
        resource_id=resource_id,
        requested_by="agent:codex",
        requested_changes={"status": "published"},
    )
    assert approval.status is ApprovalStatus.PENDING
    assert repository.list_pending() == [approval]

    repository.decide(
        approval.id, approved=True, reviewed_by="human:operator", review_note="Checked"
    )
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.reviewed_at is not None
    assert repository.list_pending() == []
    with pytest.raises(InvalidStateTransitionError):
        repository.decide(approval.id, approved=False, reviewed_by="human:other")


def test_audit_repository_appends_and_reads_in_order(session: Session) -> None:
    repository = AuditEventRepository(session)
    resource_id = uuid4()
    first = repository.append(
        event_type="inventory.created",
        actor_type="human",
        actor_id="operator",
        resource_type="inventory_item",
        resource_id=resource_id,
        details={"sku": "SKU-1"},
    )
    second = repository.append(
        event_type="inventory.quantity_changed",
        actor_type="system",
        actor_id="inventory-service",
        resource_type="inventory_item",
        resource_id=resource_id,
        details={"quantity": 2},
    )

    assert repository.list_for_resource("inventory_item", resource_id) == [first, second]
    assert not hasattr(repository, "update")
    assert not hasattr(repository, "delete")
