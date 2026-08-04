from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from goliath.agents.registry import AgentRegistry
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.marketplace_repositories import (
    CircuitBreakerRepository,
    EmergencyStopRepository,
    MarketplaceAccountRepository,
)
from goliath.db.models import (
    AgentJobStatus,
    AuditEvent,
    CircuitBreakerState,
    Marketplace,
)
from goliath.db.operations import ScheduleRepository
from goliath.db.repositories import AgentJobRepository
from goliath.marketplace.worker import MarketplaceAutomationWorker
from goliath.orchestration.scheduler import MARKETPLACE_WORKFLOWS, SchedulerService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from goliath.orchestration.worker import WorkerDaemon


@pytest.fixture
def scheduled_env(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(
        workspace_roots=[tmp_path],
        retry_fixed_delay_seconds=0,
        heartbeat_interval_seconds=1,
        lease_duration_seconds=10,
    )
    with factory() as session:
        account = MarketplaceAccountRepository(session).create(
            marketplace=Marketplace.EBAY,
            account_label="main",
            capabilities=[
                "sync_account",
                "read_listings",
                "read_listing",
                "read_orders",
                "end_listing",
                "read_offers",
                "send_message",
                "update_listing",
                "refresh_listing",
                "promote_listing",
                "purchase_label",
                "update_tracking",
            ],
        )
        account_id = account.id
        session.commit()
    scheduler = SchedulerService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory),
        orchestration_service=None,
        config=config,
    )
    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        config=config,
        scheduler=scheduler,
        account_id=account_id,
    )
    engine.dispose()


def test_default_install_is_complete_idempotent_and_configured(scheduled_env) -> None:
    first = scheduled_env.scheduler.install_marketplace_schedules()
    second = scheduled_env.scheduler.install_marketplace_schedules()
    assert first["created"] == len(MARKETPLACE_WORKFLOWS) == 21
    assert second == {
        "created": 0,
        "existing": 21,
        "schedule_ids": first["schedule_ids"],
    }
    with scheduled_env.factory() as session:
        schedules = ScheduleRepository(session).list()
    assert {row.schedule_metadata["operation_type"] for row in schedules} == {
        row.operation for row in MARKETPLACE_WORKFLOWS
    }
    assert all(int(row.timing_value) > 0 for row in schedules)
    assert all(
        row.schedule_metadata["marketplace_account_id"] == str(scheduled_env.account_id)
        for row in schedules
    )


def test_schedule_configuration_rejects_invalid_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than or equal to 5"):
        OrchestrationConfig(
            workspace_roots=[tmp_path],
            marketplace={"schedules": {"account_sync": {"interval_seconds": 0}}},
        )


def test_schedule_enable_disable_and_run_now(scheduled_env) -> None:
    scheduled_env.scheduler.install_marketplace_schedules()
    with scheduled_env.factory() as session:
        repo = ScheduleRepository(session)
        schedule = repo.list()[0]
        schedule_id = schedule.id
        repo.set_enabled(schedule_id, False)
        session.commit()
    with scheduled_env.factory() as session:
        assert ScheduleRepository(session).get(schedule_id).enabled is False
        ScheduleRepository(session).set_enabled(schedule_id, True)
        session.commit()
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    job = scheduled_env.scheduler.run_now(schedule_id, now=now)
    assert job.status is AgentJobStatus.QUEUED
    assert job.job_metadata["schedule_id"] == str(schedule_id)
    assert scheduled_env.scheduler.run_now(schedule_id, now=now) is None


def test_tick_persists_idempotent_jobs_by_window_and_account(scheduled_env) -> None:
    scheduled_env.scheduler.install_marketplace_schedules()
    tick_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    with scheduled_env.factory() as session:
        schedule = ScheduleRepository(session).list()[0]
        schedule.next_run_at = tick_at
        schedule_id = schedule.id
        session.commit()
    assert scheduled_env.scheduler.tick(tick_at) == 1
    with scheduled_env.factory() as session:
        first = ScheduleRepository(session).get(schedule_id)
        first_job_id = first.last_job_id
        first.next_run_at = tick_at
        session.commit()
    assert scheduled_env.scheduler.tick(tick_at) == 0
    with scheduled_env.factory() as session:
        schedule = ScheduleRepository(session).get(schedule_id)
        schedule.next_run_at = tick_at + timedelta(hours=1)
        session.commit()
    assert scheduled_env.scheduler.tick(tick_at + timedelta(hours=1)) == 1
    with scheduled_env.factory() as session:
        jobs = AgentJobRepository(session).list(limit=10)
        assert len(jobs) == 2
        assert first_job_id != jobs[-1].id
        metadata = jobs[0].job_metadata
        assert {
            "marketplace_account_id",
            "marketplace",
            "operation_type",
            "scheduled_timestamp",
            "schedule_id",
            "policy_version",
            "idempotency_key",
            "retry_configuration",
            "correlation_id",
            "requested_capability",
            "write_classification",
        } <= metadata.keys()
        serialized = str(metadata).lower()
        assert all(word not in serialized for word in ("cookie", "password", "authorization"))


def test_different_accounts_install_distinct_schedules(scheduled_env) -> None:
    with scheduled_env.factory() as session:
        second = MarketplaceAccountRepository(session).create(
            marketplace=Marketplace.POSHMARK,
            account_label="second",
            capabilities=["sync_account"],
        )
        second_id = second.id
        session.commit()
    result = scheduled_env.scheduler.install_marketplace_schedules()
    assert result["created"] == 42
    with scheduled_env.factory() as session:
        account_ids = {
            row.schedule_metadata["marketplace_account_id"]
            for row in ScheduleRepository(session).list()
        }
    assert account_ids == {str(scheduled_env.account_id), str(second_id)}


class _InternalWorker:
    def __init__(self, outcome: dict[str, object]) -> None:
        self.outcome = outcome
        self.calls = 0

    async def execute_job(self, metadata, *, job_id):
        self.calls += 1
        return {"operation": metadata["operation_type"], **self.outcome}


async def _claim_and_execute(env, internal_worker):
    daemon = WorkerDaemon(
        session_factory=env.factory,
        registry=AgentRegistry(),
        config=env.config,
        worker_id="marketplace-test-worker",
        marketplace_worker=internal_worker,
    )
    daemon.register()
    claimed = await daemon._claim_task()
    assert claimed is not None
    await daemon._execute(claimed)
    return claimed[0]


async def test_durable_worker_claims_executes_and_audits(scheduled_env) -> None:
    scheduled_env.scheduler.install_marketplace_schedules()
    schedule_id = UUID(scheduled_env.scheduler.install_marketplace_schedules()["schedule_ids"][0])
    scheduled_env.scheduler.run_now(schedule_id, now=datetime(2026, 8, 4, 12, 0, tzinfo=UTC))
    worker = _InternalWorker({"status": "succeeded", "executed": True})
    job_id = await _claim_and_execute(scheduled_env, worker)
    with scheduled_env.factory() as session:
        job = AgentJobRepository(session).get(job_id)
        events = list(session.query(AuditEvent).filter(AuditEvent.resource_id == job_id).all())
    assert worker.calls == 1
    assert job.status is AgentJobStatus.SUCCEEDED
    assert job.structured_result["status"] == "succeeded"
    assert any(event.event_type == "execution.succeeded" for event in events)


async def test_permanent_skip_is_not_retried_and_transient_failure_is(scheduled_env) -> None:
    scheduled_env.scheduler.install_marketplace_schedules()
    ids = scheduled_env.scheduler.install_marketplace_schedules()["schedule_ids"]
    first_id, second_id = map(UUID, ids[:2])
    scheduled_env.scheduler.run_now(first_id, now=datetime(2026, 8, 4, 12, 0, tzinfo=UTC))
    skipped = _InternalWorker({"status": "skipped", "reason": "unsupported_capability"})
    skipped_job_id = await _claim_and_execute(scheduled_env, skipped)
    with scheduled_env.factory() as session:
        assert AgentJobRepository(session).get(skipped_job_id).status is AgentJobStatus.SUCCEEDED

    scheduled_env.scheduler.run_now(second_id, now=datetime(2026, 8, 4, 12, 1, tzinfo=UTC))
    # Reuse a distinct worker identity because worker registrations are durable.
    daemon = WorkerDaemon(
        session_factory=scheduled_env.factory,
        registry=AgentRegistry(),
        config=scheduled_env.config,
        worker_id="marketplace-retry-worker",
        marketplace_worker=_InternalWorker({"status": "failed", "reason": "transient"}),
    )
    daemon.register()
    claimed = await daemon._claim_task()
    await daemon._execute(claimed)
    with scheduled_env.factory() as session:
        job = AgentJobRepository(session).get(claimed[0])
        assert job.status is AgentJobStatus.QUEUED
        assert job.next_eligible_at is not None


class _Receipt:
    def __init__(self, *, ok=True, category=None):
        self.ok = ok
        self.executed = False
        self.verified = False
        self.error_category = category
        self.reasons = [] if ok else [category or "failure"]


class _WorkflowService:
    def __init__(self, factory) -> None:
        self._session_factory = factory
        self._marketplace = SimpleNamespace(circuit_breaker=SimpleNamespace(cooldown_seconds=60))
        self.health_ok = True

    async def read_listings_remote(self, *args, **kwargs):
        return _Receipt()

    async def health_check_remote(self, *args, **kwargs):
        return _Receipt(ok=self.health_ok, category=None if self.health_ok else "transient")

    def get_account(self, account_id):
        with self._session_factory() as session:
            return MarketplaceAccountRepository(session).require(account_id)


def _metadata(account_id, operation, capability, classification):
    return {
        "marketplace_account_id": str(account_id),
        "operation_type": operation,
        "requested_capability": capability,
        "write_classification": classification,
    }


async def test_stop_blocks_writes_but_reads_continue_and_accounts_are_isolated(
    scheduled_env,
) -> None:
    service = _WorkflowService(scheduled_env.factory)
    worker = MarketplaceAutomationWorker(service)
    with scheduled_env.factory() as session:
        EmergencyStopRepository(session).activate(
            scope_key=str(scheduled_env.account_id), reason="test", actor="human:test"
        )
        second = MarketplaceAccountRepository(session).create(
            marketplace=Marketplace.POSHMARK,
            account_label="other",
            capabilities=["update_listing"],
        )
        second_id = second.id
        session.commit()
    read = await worker.execute_job(
        _metadata(scheduled_env.account_id, "listing_state_sync", "read_listings", "read_only"),
        job_id=UUID(int=1),
    )
    blocked = await worker.execute_job(
        _metadata(scheduled_env.account_id, "price_automation", "update_listing", "write_capable"),
        job_id=UUID(int=2),
    )
    isolated = await worker.execute_job(
        _metadata(second_id, "price_automation", "update_listing", "write_capable"),
        job_id=UUID(int=3),
    )
    assert read["status"] == "succeeded"
    assert blocked == {
        "status": "blocked",
        "operation": "price_automation",
        "reason": "emergency_stop",
        "executed": False,
    }
    assert isolated["status"] == "succeeded"


async def test_operation_breaker_isolation_and_half_open_probe(scheduled_env) -> None:
    service = _WorkflowService(scheduled_env.factory)
    worker = MarketplaceAutomationWorker(service)
    with scheduled_env.factory() as session:
        repo = CircuitBreakerRepository(session)
        breaker = repo.get_or_create(
            scope="operation",
            scope_key=f"{scheduled_env.account_id}:price_automation",
            threshold=1,
        )
        breaker.state = CircuitBreakerState.OPEN
        breaker.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    blocked = await worker.execute_job(
        _metadata(
            scheduled_env.account_id,
            "price_automation",
            "update_listing",
            "write_capable",
        ),
        job_id=UUID(int=4),
    )
    unrelated = await worker.execute_job(
        _metadata(
            scheduled_env.account_id,
            "listing_refresh",
            "refresh_listing",
            "write_capable",
        ),
        job_id=UUID(int=5),
    )
    probe = await worker.execute_job(
        _metadata(
            scheduled_env.account_id,
            "circuit_breaker_probe",
            "health_check",
            "read_only",
        ),
        job_id=UUID(int=6),
    )
    assert blocked["status"] == "blocked"
    assert unrelated["status"] == "succeeded"
    assert probe["status"] == "succeeded"
    with scheduled_env.factory() as session:
        state = next(
            row.state
            for row in CircuitBreakerRepository(session).list()
            if row.scope_key.endswith(":price_automation")
        )
    assert state is CircuitBreakerState.CLOSED


async def test_failed_half_open_probe_reopens_breaker(scheduled_env) -> None:
    service = _WorkflowService(scheduled_env.factory)
    service.health_ok = False
    worker = MarketplaceAutomationWorker(service)
    with scheduled_env.factory() as session:
        repo = CircuitBreakerRepository(session)
        breaker = repo.get_or_create(
            scope="account", scope_key=str(scheduled_env.account_id), threshold=1
        )
        breaker.state = CircuitBreakerState.OPEN
        breaker.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    result = await worker.execute_job(
        _metadata(
            scheduled_env.account_id,
            "circuit_breaker_probe",
            "health_check",
            "read_only",
        ),
        job_id=UUID(int=7),
    )
    assert result["status"] == "failed"
    with scheduled_env.factory() as session:
        breaker = next(iter(CircuitBreakerRepository(session).list()))
        assert breaker.state is CircuitBreakerState.OPEN
        assert breaker.next_probe_at is not None
