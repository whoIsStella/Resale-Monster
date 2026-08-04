from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from goliath.agents.registry import AgentRegistry
from goliath.agents.router import AgentRouter, NoEligibleAgentError
from goliath.agents.supervisor import terminate_external_process_group
from goliath.config import OrchestrationConfig
from goliath.core.schemas import (
    CancellationRequest,
    ExecutionRequest,
    ExecutionResult,
    JobResponse,
    JobStatus,
    JobSubmission,
)
from goliath.db.models import AgentJobRecord, AgentJobStatus
from goliath.db.repositories import InvalidStateTransitionError, RecordNotFoundError
from goliath.orchestration.uow import JobUnitOfWork


class OrchestrationError(RuntimeError):
    pass


class JobOrchestrationService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], JobUnitOfWork],
        registry: AgentRegistry,
        config: OrchestrationConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._router = AgentRouter(registry)
        self._config = config

    async def submit(self, submission: JobSubmission) -> JobResponse:
        workspace = self._config.validate_workspace(submission.workspace)
        if submission.timeout_seconds > self._config.maximum_timeout_seconds:
            raise OrchestrationError("timeout exceeds configured maximum")
        if submission.max_output_chars > self._config.maximum_output_limit_chars:
            raise OrchestrationError("output limit exceeds configured maximum")

        job_id = uuid4()
        with self._uow_factory() as uow:
            record = uow.jobs.create(
                job_id=job_id,
                requested_agent=submission.requested_agent,
                task_type=submission.task_type.value,
                objective=submission.objective,
                workspace_path=str(workspace),
                permissions=sorted(permission.value for permission in submission.permissions),
                context_files=[str(path) for path in submission.context_files],
                forbidden_actions=sorted(submission.forbidden_actions),
                risk_tier=submission.risk_tier.value,
                timeout_seconds=submission.timeout_seconds,
                max_output_chars=submission.max_output_chars,
                job_metadata=submission.metadata,
            )
            self._audit(
                uow, record, "job.submitted", {"requested_agent": submission.requested_agent}
            )

        try:
            adapter = await self._router.route(submission)
        except NoEligibleAgentError as error:
            with self._uow_factory() as uow:
                record = self._require(uow, job_id)
                record = uow.jobs.transition(
                    job_id,
                    AgentJobStatus.FAILED,
                    expected_version=record.version,
                    failure_reason=str(error),
                )
                self._audit(uow, record, "agent.no_eligible", {"reason": str(error)})
                self._audit(uow, record, "execution.failed", {"reason": str(error)})
                return JobResponse.model_validate(record)

        with self._uow_factory() as uow:
            record = self._require(uow, job_id)
            selected_agent = adapter.name
            self._audit(uow, record, "agent.selected", {"agent": selected_agent})
            record = uow.jobs.transition(
                job_id,
                AgentJobStatus.QUEUED,
                expected_version=record.version,
                agent_name=selected_agent,
            )
            self._audit(uow, record, "job.queued", {"agent": selected_agent})
            return JobResponse.model_validate(record)

    async def run_next(self) -> JobResponse | None:
        with self._uow_factory() as uow:
            record = uow.jobs.claim_next()
            if record is None:
                return None
            self._audit(uow, record, "execution.started", {"agent": record.agent_name})
            request = self._execution_request(record)

        adapter = self._registry.get(request.agent)
        if adapter is None:
            result = ExecutionResult(
                job_id=request.job_id,
                agent=request.agent,
                duration_seconds=0,
                failure_reason=f"selected agent is no longer configured: {request.agent}",
            )
        else:
            try:
                result = await adapter.run(
                    request,
                    on_started=lambda process_id: self._record_process_id(
                        request.job_id, process_id
                    ),
                )
            except Exception as error:  # noqa: BLE001 - adapter boundary must persist failures
                result = ExecutionResult(
                    job_id=request.job_id,
                    agent=request.agent,
                    duration_seconds=0,
                    failure_reason=f"adapter failure: {type(error).__name__}: {error}",
                )
        return self._finish(result)

    async def cancel(self, job_id: UUID, request: CancellationRequest) -> JobResponse:
        running_agent: str | None = None
        running_process_id: int | None = None
        transition_error: InvalidStateTransitionError | None = None
        with self._uow_factory() as uow:
            record = self._require(uow, job_id)
            self._audit(
                uow,
                record,
                "cancellation.requested",
                {"requested_by": request.requested_by, "reason": request.reason},
            )
            if record.status not in {
                AgentJobStatus.PENDING,
                AgentJobStatus.QUEUED,
                AgentJobStatus.RUNNING,
            }:
                self._audit(
                    uow,
                    record,
                    "job.invalid_transition",
                    {"from": record.status.value, "to": AgentJobStatus.CANCELLED.value},
                )
                transition_error = InvalidStateTransitionError(
                    f"invalid agent job transition: {record.status.value} -> cancelled"
                )
            elif record.status is AgentJobStatus.RUNNING:
                running_agent = record.agent_name
                running_process_id = record.process_id
                record = uow.jobs.transition(
                    job_id,
                    AgentJobStatus.CANCELLED,
                    expected_version=record.version,
                    cancellation_reason=request.reason,
                )
                self._audit(uow, record, "execution.cancelled", {"reason": request.reason})
            else:
                record = uow.jobs.transition(
                    job_id,
                    AgentJobStatus.CANCELLED,
                    expected_version=record.version,
                    cancellation_reason=request.reason,
                )
                self._audit(uow, record, "execution.cancelled", {"reason": request.reason})
                return JobResponse.model_validate(record)

        if transition_error is not None:
            raise transition_error

        if running_agent:
            adapter = self._registry.get(running_agent)
            cancelled_locally = adapter is not None and await adapter.cancel(job_id)
            cancelled_externally = False
            if not cancelled_locally and running_process_id is not None:
                cancelled_externally = await terminate_external_process_group(
                    running_process_id,
                    self._config.cancellation_grace_seconds,
                )
            if not cancelled_locally and not cancelled_externally:
                with self._uow_factory() as uow:
                    record = self._require(uow, job_id)
                    self._audit(
                        uow,
                        record,
                        "cancellation.signal_failed",
                        {"process_id": running_process_id},
                    )
                raise OrchestrationError("job was cancelled but its process could not be signalled")
        return self.get(job_id)

    def get(self, job_id: UUID) -> JobResponse:
        with self._uow_factory() as uow:
            return JobResponse.model_validate(self._require(uow, job_id))

    def list(self, *, status: JobStatus | None = None, limit: int = 100) -> list[JobResponse]:
        database_status = AgentJobStatus(status.value) if status else None
        with self._uow_factory() as uow:
            return [
                JobResponse.model_validate(record)
                for record in uow.jobs.list(status=database_status, limit=limit)
            ]

    def _finish(self, result: ExecutionResult) -> JobResponse:
        with self._uow_factory() as uow:
            record = self._require(uow, result.job_id)
            uow.jobs.store_execution_result(
                result.job_id,
                process_id=result.process_id,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                structured_result=result.structured_result,
                failure_reason=result.failure_reason,
                timed_out=result.timed_out,
                output_truncated=result.stdout_truncated or result.stderr_truncated,
                stdout_total_chars=result.stdout_total_chars,
                stderr_total_chars=result.stderr_total_chars,
            )
            if result.stdout_truncated or result.stderr_truncated:
                self._audit(
                    uow,
                    record,
                    "execution.output_truncated",
                    {
                        "stdout_total_chars": result.stdout_total_chars,
                        "stderr_total_chars": result.stderr_total_chars,
                        "limit": record.max_output_chars,
                    },
                )
            if record.status is AgentJobStatus.CANCELLED:
                return JobResponse.model_validate(record)
            if result.cancelled:
                target = AgentJobStatus.CANCELLED
                event_type = "execution.cancelled"
            elif result.timed_out:
                target = AgentJobStatus.TIMED_OUT
                event_type = "execution.timed_out"
            elif result.succeeded:
                target = AgentJobStatus.SUCCEEDED
                event_type = "execution.succeeded"
            else:
                target = AgentJobStatus.FAILED
                event_type = "execution.failed"
            record = uow.jobs.transition(
                result.job_id,
                target,
                expected_version=record.version,
                failure_reason=result.failure_reason,
                cancellation_reason="process cancelled" if result.cancelled else None,
            )
            self._audit(
                uow,
                record,
                event_type,
                {"exit_code": result.exit_code, "process_id": result.process_id},
            )
            return JobResponse.model_validate(record)

    def _record_process_id(self, job_id: UUID, process_id: int) -> None:
        with self._uow_factory() as uow:
            uow.jobs.set_process_id(job_id, process_id)

    @staticmethod
    def _execution_request(record: AgentJobRecord) -> ExecutionRequest:
        if record.agent_name is None:
            raise OrchestrationError("queued job has no selected agent")
        return ExecutionRequest(
            job_id=record.id,
            agent=record.agent_name,
            task_type=record.task_type,
            objective=record.objective,
            workspace=Path(record.workspace_path),
            permissions=set(record.permissions),
            context_files=[Path(path) for path in record.context_files],
            forbidden_actions=set(record.forbidden_actions),
            timeout_seconds=record.timeout_seconds,
            max_output_chars=record.max_output_chars,
            metadata=record.job_metadata,
        )

    @staticmethod
    def _require(uow: JobUnitOfWork, job_id: UUID) -> AgentJobRecord:
        record = uow.jobs.get(job_id)
        if record is None:
            raise RecordNotFoundError(f"agent job not found: {job_id}")
        return record

    @staticmethod
    def _audit(
        uow: JobUnitOfWork,
        record: AgentJobRecord,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        uow.audits.append(
            event_type=event_type,
            actor_type="system",
            actor_id="agent-orchestrator",
            resource_type="agent_job",
            resource_id=record.id,
            details=details,
        )
