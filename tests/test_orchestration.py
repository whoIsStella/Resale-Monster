import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from goliath.agents.registry import AgentRegistry
from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.agents.supervisor import ProcessSupervisor
from goliath.config import OrchestrationConfig
from goliath.core.schemas import (
    REQUIRED_FORBIDDEN_ACTIONS,
    AgentCapability,
    AgentHealthResult,
    CancellationRequest,
    ExecutionRequest,
    ExecutionResult,
    JobStatus,
    JobSubmission,
)
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.repositories import AuditEventRepository, InvalidStateTransitionError
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork


class FakeAdapter:
    def __init__(self, name: str = "fake", *, available: bool = True) -> None:
        self.name = name
        self.available = available
        self._capabilities = (
            AgentCapability(
                name="coding",
                task_types={"code_change", "test"},
                permissions={"read_files", "write_files", "run_commands"},
            ),
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_for_release = False
        self.cancelled = False

    @property
    def capabilities(self) -> tuple[AgentCapability, ...]:
        return self._capabilities

    async def healthcheck(self) -> AgentHealthResult:
        return AgentHealthResult(
            agent=self.name,
            available=self.available,
            executable=self.name,
            detail=None if self.available else "executable not found",
        )

    async def run(
        self,
        request: ExecutionRequest,
        on_started: Callable[[int], None] | None = None,
    ) -> ExecutionResult:
        self.started.set()
        if on_started is not None:
            on_started(123)
        if self.wait_for_release:
            await self.release.wait()
        return ExecutionResult(
            job_id=request.job_id,
            agent=self.name,
            process_id=123,
            exit_code=None if self.cancelled else 0,
            stdout='{"summary": "done"}',
            duration_seconds=0.01,
            cancelled=self.cancelled,
            structured_result={"summary": "done"},
            stdout_total_chars=19,
        )

    async def cancel(self, job_id: UUID) -> bool:
        self.cancelled = True
        self.release.set()
        return True


def build_service(tmp_path: Path, adapter: FakeAdapter):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    registry = AgentRegistry()
    registry.register(adapter.name, adapter, priority=1)
    config = OrchestrationConfig(workspace_roots=[tmp_path], agents={})
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(session_factory),
        registry=registry,
        config=config,
    )
    return service, session_factory, engine


def submission(tmp_path: Path, *, agent: str | None = "fake") -> JobSubmission:
    return JobSubmission(
        requested_agent=agent,
        task_type="code_change",
        objective="Implement safely",
        workspace=tmp_path,
        permissions={"read_files", "write_files"},
        required_capabilities={"coding"},
        forbidden_actions=set(REQUIRED_FORBIDDEN_ACTIONS),
        timeout_seconds=10,
        max_output_chars=1_000,
    )


@pytest.mark.asyncio
async def test_successful_execution_persists_results_and_audits(tmp_path: Path) -> None:
    service, factory, engine = build_service(tmp_path, FakeAdapter())
    queued = await service.submit(submission(tmp_path))
    assert queued.status is JobStatus.QUEUED

    result = await service.run_next()
    assert result is not None
    assert result.status is JobStatus.SUCCEEDED
    assert result.process_id == 123
    assert result.stdout == '{"summary": "done"}'
    assert result.structured_result == {"summary": "done"}

    with factory() as session:
        events = AuditEventRepository(session).list_for_resource("agent_job", result.id)
        assert [event.event_type for event in events] == [
            "job.submitted",
            "agent.selected",
            "job.queued",
            "execution.started",
            "execution.succeeded",
        ]
    engine.dispose()


@pytest.mark.asyncio
async def test_no_eligible_agent_fails_job_and_audits(tmp_path: Path) -> None:
    service, factory, engine = build_service(tmp_path, FakeAdapter(available=False))
    result = await service.submit(submission(tmp_path))
    assert result.status is JobStatus.FAILED
    assert "not eligible" in (result.failure_reason or "")
    with factory() as session:
        event_types = [
            event.event_type
            for event in AuditEventRepository(session).list_for_resource("agent_job", result.id)
        ]
    assert "agent.no_eligible" in event_types
    assert "execution.failed" in event_types
    engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_execution_is_prevented(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    adapter.wait_for_release = True
    service, _, engine = build_service(tmp_path, adapter)
    await service.submit(submission(tmp_path))
    first = asyncio.create_task(service.run_next())
    await adapter.started.wait()
    assert await service.run_next() is None
    adapter.release.set()
    assert (await first).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]
    engine.dispose()


@pytest.mark.asyncio
async def test_running_job_cancellation_is_persisted_and_audited(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    adapter.wait_for_release = True
    service, factory, engine = build_service(tmp_path, adapter)
    queued = await service.submit(submission(tmp_path))
    running = asyncio.create_task(service.run_next())
    await adapter.started.wait()
    cancelled = await service.cancel(
        queued.id,
        CancellationRequest(reason="Operator stopped it", requested_by="human:test"),
    )
    final = await running
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancellation_reason == "Operator stopped it"
    assert final is not None and final.status is JobStatus.CANCELLED
    with pytest.raises(InvalidStateTransitionError):
        await service.cancel(queued.id, CancellationRequest(reason="Too late"))
    with factory() as session:
        event_types = [
            event.event_type
            for event in AuditEventRepository(session).list_for_resource("agent_job", queued.id)
        ]
    assert "cancellation.requested" in event_types
    assert "execution.cancelled" in event_types
    assert "job.invalid_transition" in event_types
    engine.dispose()


@pytest.mark.asyncio
async def test_output_truncation_audit_is_written(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    async def truncated(
        request: ExecutionRequest, on_started: Callable[[int], None] | None = None
    ) -> ExecutionResult:
        return ExecutionResult(
            job_id=request.job_id,
            agent=adapter.name,
            exit_code=0,
            stdout="short",
            duration_seconds=0.1,
            stdout_truncated=True,
            stdout_total_chars=5_000,
        )

    adapter.run = truncated  # type: ignore[method-assign]
    service, factory, engine = build_service(tmp_path, adapter)
    queued = await service.submit(submission(tmp_path))
    await service.run_next()
    with factory() as session:
        event_types = [
            event.event_type
            for event in AuditEventRepository(session).list_for_resource("agent_job", queued.id)
        ]
    assert "execution.output_truncated" in event_types
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_event"),
    [
        ("failed", JobStatus.FAILED, "execution.failed"),
        ("timed_out", JobStatus.TIMED_OUT, "execution.timed_out"),
        ("adapter_error", JobStatus.FAILED, "execution.failed"),
    ],
)
async def test_execution_failures_and_timeouts_are_persisted_and_audited(
    tmp_path: Path,
    mode: str,
    expected_status: JobStatus,
    expected_event: str,
) -> None:
    adapter = FakeAdapter()

    async def terminal(
        request: ExecutionRequest, on_started: Callable[[int], None] | None = None
    ) -> ExecutionResult:
        if mode == "adapter_error":
            raise RuntimeError("adapter exploded")
        return ExecutionResult(
            job_id=request.job_id,
            agent=adapter.name,
            exit_code=124 if mode == "timed_out" else 2,
            duration_seconds=0.1,
            timed_out=mode == "timed_out",
            failure_reason=None if mode == "timed_out" else "process failed",
        )

    adapter.run = terminal  # type: ignore[method-assign]
    service, factory, engine = build_service(tmp_path, adapter)
    queued = await service.submit(submission(tmp_path))
    result = await service.run_next()
    assert result is not None and result.status is expected_status
    if mode == "adapter_error":
        assert "adapter failure" in (result.failure_reason or "")
    with factory() as session:
        event_types = [
            event.event_type
            for event in AuditEventRepository(session).list_for_resource("agent_job", queued.id)
        ]
    assert expected_event in event_types
    engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_from_separate_registry_uses_persisted_process_group(
    tmp_path: Path,
) -> None:
    capability = AgentCapability(
        name="coding",
        task_types={"code_change"},
        permissions={"read_files", "write_files"},
    )
    worker_adapter = SubprocessAgentAdapter(
        name="fake",
        command_template=[sys.executable, "-c", "import time; time.sleep(20)"],
        supervisor=ProcessSupervisor(cancellation_grace_seconds=0.1),
        _capabilities=(capability,),
    )
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(
        workspace_roots=[tmp_path],
        cancellation_grace_seconds=0.1,
    )
    worker_registry = AgentRegistry()
    worker_registry.register("fake", worker_adapter)
    worker_service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory),
        registry=worker_registry,
        config=config,
    )
    separate_adapter = SubprocessAgentAdapter(
        name="fake",
        command_template=[sys.executable, "-c", "import time; time.sleep(20)"],
        supervisor=ProcessSupervisor(cancellation_grace_seconds=0.1),
        _capabilities=(capability,),
    )
    separate_registry = AgentRegistry()
    separate_registry.register("fake", separate_adapter)
    cancelling_service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory),
        registry=separate_registry,
        config=config,
    )

    queued = await worker_service.submit(submission(tmp_path))
    running = asyncio.create_task(worker_service.run_next())
    for _ in range(100):
        if worker_service.get(queued.id).process_id is not None:
            break
        await asyncio.sleep(0.01)
    assert worker_service.get(queued.id).process_id is not None
    cancelled = await cancelling_service.cancel(
        queued.id, CancellationRequest(reason="Cross-process cancellation")
    )
    final = await asyncio.wait_for(running, timeout=2)
    assert cancelled.status is JobStatus.CANCELLED
    assert final is not None and final.status is JobStatus.CANCELLED
    engine.dispose()
