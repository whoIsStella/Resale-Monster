"""Milestone five: review-task concurrency, automation, bulk ops, scopes, redaction."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from goliath.config import OrchestrationConfig
from goliath.core.schemas import HUMAN_ONLY_SCOPES
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.domain_repositories import McpPrincipalRepository
from goliath.db.ingestion_repositories import ReviewTaskRepository
from goliath.db.models import ReviewTaskStatus, ReviewTaskType
from goliath.db.repositories import VersionConflictError
from goliath.domain.review_service import ReviewService
from goliath.domain.service import DomainService


@pytest.fixture
def env(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media.media_root = tmp_path / "m"
    config.domain.media.quarantine_root = tmp_path / "q"
    config.domain.media.temp_upload_root = tmp_path / "t"
    yield {
        "factory": factory,
        "config": config,
        "domain": DomainService(session_factory=factory, config=config),
        "review": ReviewService(session_factory=factory, config=config),
    }
    engine.dispose()


def _task(session):
    task, _ = ReviewTaskRepository(session).create_idempotent(
        task_type=ReviewTaskType.COMPARABLE_REVIEW,
        resource_type="comparable_sale",
        resource_id=uuid4(),
        reason="needs review",
    )
    return task


def test_review_task_creation_is_idempotent(env) -> None:
    resource_id = uuid4()
    with env["factory"]() as session:
        repo = ReviewTaskRepository(session)
        first, created_a = repo.create_idempotent(
            task_type=ReviewTaskType.IMAGE_QUALITY,
            resource_type="inventory_media",
            resource_id=resource_id,
            reason="blur",
        )
        second, created_b = repo.create_idempotent(
            task_type=ReviewTaskType.IMAGE_QUALITY,
            resource_type="inventory_media",
            resource_id=resource_id,
            reason="blur",
        )
        session.commit()
    assert created_a is True
    assert created_b is False
    assert first.id == second.id


def test_atomic_claim_rejects_stale_version(env) -> None:
    with env["factory"]() as session:
        task = _task(session)
        session.commit()
        task_id, version = task.id, task.version
    env["review"].claim_task(task_id, reviewer="alice", expected_version=version)
    # A second reviewer using the old version loses the race.
    with pytest.raises(VersionConflictError):
        env["review"].claim_task(task_id, reviewer="bob", expected_version=version)


def test_completeness_blocking_creates_task(env) -> None:
    item = env["domain"].create_inventory(
        actor="human:op",
        sku="I1",
        title="Tee",
        condition="good",
        acquisition_cost=Decimal(5),
        category="clothing",
    )
    env["domain"].evaluate_completeness(item.id)
    tasks = env["review"].list_tasks(task_type=ReviewTaskType.INVENTORY_COMPLETION)
    assert len(tasks) == 1


def test_proposal_creates_review_task(env) -> None:
    from goliath.db.models import ProposalType

    item = env["domain"].create_inventory(
        actor="human:op", sku="I2", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    env["domain"].submit_proposal(
        actor="service_principal:hermes",
        proposal_type=ProposalType.INVENTORY_UPDATE,
        resource_type="inventory_item",
        resource_id=item.id,
        current_version=item.version,
        proposed_payload={"brand": "Nike"},
        justification="id",
    )
    tasks = env["review"].list_tasks(task_type=ReviewTaskType.PROPOSAL_REVIEW)
    assert len(tasks) == 1


def test_task_lifecycle_claim_complete(env) -> None:
    with env["factory"]() as session:
        task = _task(session)
        session.commit()
        task_id, version = task.id, task.version
    env["review"].claim_task(task_id, reviewer="alice", expected_version=version)
    completed = env["review"].complete_task(task_id, reviewer="alice", outcome="resolved")
    assert completed.status is ReviewTaskStatus.COMPLETED


def test_bulk_complete_reports_per_item(env) -> None:
    ids = []
    with env["factory"]() as session:
        for _ in range(3):
            ids.append(_task(session).id)
        session.commit()
    ids.append(uuid4())  # one that does not exist
    result = env["review"].bulk_complete_tasks(ids, reviewer="alice", outcome="resolved")
    assert result["succeeded"] == 3
    assert result["failed"] == 1
    assert len(result["results"]) == 4


def test_bulk_batch_size_is_bounded(env) -> None:
    env["config"].domain.bulk_operation_max_items = 2
    from goliath.domain.review_service import ReviewError

    with pytest.raises(ReviewError):
        env["review"].bulk_complete_tasks([uuid4(), uuid4(), uuid4()], reviewer="a", outcome="x")


def test_task_expiration(env) -> None:
    from datetime import UTC, datetime, timedelta

    with env["factory"]() as session:
        repo = ReviewTaskRepository(session)
        task, _ = repo.create_idempotent(
            task_type=ReviewTaskType.PRICING_REVIEW,
            resource_type="inventory_item",
            resource_id=uuid4(),
            reason="low confidence",
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.commit()
        task_id = task.id
    assert env["review"].expire_due_tasks() == 1
    assert env["review"].get_task(task_id).status is ReviewTaskStatus.EXPIRED


def test_service_principal_cannot_hold_human_only_scopes(env) -> None:
    with env["factory"]() as session, pytest.raises(ValueError, match="human-only"):
        McpPrincipalRepository(session).create(
            name="bad", scopes=["inventory:read", *HUMAN_ONLY_SCOPES]
        )


def test_dashboard_summary_aggregates(env) -> None:
    env["domain"].create_inventory(
        actor="human:op", sku="D1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    summary = env["review"].summary()
    assert "inventory_by_status" in summary
    assert "review_tasks_by_status" in summary
    assert summary["media_failures"] == 0


def test_audit_events_never_contain_credentials(env) -> None:
    """Redaction: no audit event stores raw secrets, tokens, or file bytes."""
    from goliath.db.models import AuditEvent

    with env["factory"]() as session:
        _, raw = McpPrincipalRepository(session).create(name="p", scopes=["inventory:read"])
        session.commit()
    item = env["domain"].create_inventory(
        actor="human:op", sku="A1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    env["domain"].evaluate_completeness(item.id)
    with env["factory"]() as session:
        events = session.query(AuditEvent).all()
        blob = " ".join(str(e.details) for e in events)
    assert raw not in blob
    assert "credential_hash" not in blob
