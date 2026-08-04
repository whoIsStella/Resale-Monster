from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from goliath.agents.registry import AgentRegistry
from goliath.config import OrchestrationConfig
from goliath.core.schemas import ExecutionRequest, ExecutionResult
from goliath.db.models import AgentJobStatus, ExecutionAttempt, WorkerStatus, utc_now
from goliath.db.operations import ExecutionAttemptRepository, LeaseRepository, WorkerRepository
from goliath.db.repositories import (
    AgentJobRepository,
    AuditEventRepository,
    InvalidStateTransitionError,
)

logger = logging.getLogger("goliath.worker")


class WorkerDaemon:
    def __init__(
        self,
        *,
        session_factory,
        registry: AgentRegistry,
        config: OrchestrationConfig,
        worker_id: str | None = None,
        marketplace_worker=None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.config = config
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.marketplace_worker = marketplace_worker
        self._marketplace_tick_running = False
        self._stop = asyncio.Event()
        self._draining = False
        self._tasks: set[asyncio.Task[Any]] = set()

    def request_shutdown(self) -> None:
        self._draining = True
        self._stop.set()

    def register(self) -> None:
        with self.session_factory() as session:
            WorkerRepository(session).register(
                self.worker_id,
                socket.gethostname(),
                os.getpid(),
                self.config.worker_concurrency,
                "0.1.0",
            )
            session.commit()

    def heartbeat(self) -> None:
        with self.session_factory() as session:
            WorkerRepository(session).heartbeat(self.worker_id, len(self._tasks))
            session.commit()

    async def run(self) -> None:
        self.register()
        logger.info("worker_started", extra={"worker_id": self.worker_id})
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._draining:
                if self.marketplace_worker is not None and not self._marketplace_tick_running:
                    self._marketplace_tick_running = True
                    try:
                        await self.marketplace_worker.run_once()
                    except Exception:
                        logger.exception("marketplace_maintenance_failed")
                    finally:
                        self._marketplace_tick_running = False
                self._tasks = {task for task in self._tasks if not task.done()}
                while len(self._tasks) < self.config.worker_concurrency and not self._draining:
                    task = await self._claim_task()
                    if task is None:
                        break
                    self._tasks.add(asyncio.create_task(self._execute(task)))
                if self._tasks:
                    await asyncio.sleep(self.config.worker_poll_interval_seconds)
                else:
                    await asyncio.sleep(self.config.worker_poll_interval_seconds)
            if self._tasks:
                await asyncio.wait(self._tasks, timeout=self.config.shutdown_grace_seconds)
        finally:
            heartbeat_task.cancel()
            with self.session_factory() as session:
                WorkerRepository(session).set_status(self.worker_id, WorkerStatus.STOPPED)
                session.commit()
            logger.info("worker_stopped", extra={"worker_id": self.worker_id})

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            self.heartbeat()

    async def _claim_task(self):
        with self.session_factory() as session:
            claimed = LeaseRepository(session).acquire(
                self.worker_id, self.config.lease_duration_seconds
            )
            if claimed is None:
                return None
            record, lease, token = claimed
            attempt = ExecutionAttemptRepository(session).create(
                record.id, self.worker_id, lease.lease_identifier, lease.attempt_number
            )
            AuditEventRepository(session).append(
                event_type="execution.started",
                actor_type="worker",
                actor_id=self.worker_id,
                resource_type="agent_job",
                resource_id=record.id,
                details={
                    "lease_identifier": lease.lease_identifier,
                    "attempt": attempt.attempt_number,
                },
            )
            session.commit()
            return record.id, lease.lease_identifier, token, record.agent_name

    async def _execute(self, claimed) -> None:
        job_id, lease_identifier, token, agent_name = claimed
        with self.session_factory() as session:
            record = AgentJobRepository(session).get(job_id)
            if record is None or agent_name is None:
                return
            request = ExecutionRequest(
                job_id=record.id,
                agent=agent_name,
                task_type=record.task_type,
                objective=record.objective,
                workspace=record.workspace_path,
                permissions=set(record.permissions),
                context_files=record.context_files,
                forbidden_actions=set(record.forbidden_actions),
                timeout_seconds=record.timeout_seconds,
                max_output_chars=record.max_output_chars,
                metadata=record.job_metadata,
            )
        adapter = self.registry.get(agent_name)
        if adapter is None:
            result = ExecutionResult(
                job_id=job_id,
                agent=agent_name,
                duration_seconds=0,
                failure_reason="selected agent unavailable",
            )
        else:
            heartbeat = asyncio.create_task(self._lease_heartbeat(job_id, token))
            try:
                result = await adapter.run(request, on_started=lambda pid: self._set_pid(job_id, pid))
            except Exception as error:  # noqa: BLE001
                result = ExecutionResult(job_id=job_id, agent=agent_name, duration_seconds=0, failure_reason=f"adapter failure: {error}")
            finally:
                heartbeat.cancel()
        self._complete(job_id, lease_identifier, token, result)

    async def _lease_heartbeat(self, job_id, token: str) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            with self.session_factory() as session:
                LeaseRepository(session).heartbeat(job_id, self.worker_id, token, self.config.lease_duration_seconds)
                session.commit()

    def _set_pid(self, job_id, pid: int) -> None:
        with self.session_factory() as session:
            record = AgentJobRepository(session).get(job_id)
            if record:
                record.process_id = pid
                session.commit()

    def _complete(self, job_id, lease_identifier: str, token: str, result: ExecutionResult) -> None:
        with self.session_factory() as session:
            leases = LeaseRepository(session)
            lease = leases.get(job_id)
            if lease is None or lease.lease_identifier != lease_identifier:
                raise InvalidStateTransitionError("lease changed before completion")
            leases.validate(job_id, self.worker_id, token)
            jobs = AgentJobRepository(session)
            record = jobs.get(job_id)
            if record is None:
                return
            jobs.store_execution_result(
                job_id,
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
            target = (
                AgentJobStatus.TIMED_OUT
                if result.timed_out
                else AgentJobStatus.SUCCEEDED
                if result.succeeded
                else AgentJobStatus.FAILED
            )
            retryable = target is AgentJobStatus.FAILED and (
                result.exit_code in self.config.retryable_exit_codes
                or result.failure_reason
                and "adapter" in result.failure_reason
            )
            if retryable and lease.attempt_number < self.config.retry_max_attempts:
                delay = min(
                    self.config.retry_max_delay_seconds,
                    self.config.retry_fixed_delay_seconds
                    * (2 ** (lease.attempt_number - 1) if self.config.retry_exponential else 1),
                )
                record = jobs.transition(
                    job_id,
                    AgentJobStatus.QUEUED,
                    expected_version=record.version,
                    failure_reason=result.failure_reason,
                )
                record.next_eligible_at = utc_now() + timedelta(seconds=delay)
                AuditEventRepository(session).append(
                    event_type="execution.retry_scheduled",
                    actor_type="worker",
                    actor_id=self.worker_id,
                    resource_type="agent_job",
                    resource_id=job_id,
                    details={"attempt": lease.attempt_number, "delay_seconds": delay},
                )
            else:
                record = jobs.transition(
                    job_id,
                    target,
                    expected_version=record.version,
                    failure_reason=result.failure_reason,
                )
                AuditEventRepository(session).append(
                    event_type=f"execution.{target.value}",
                    actor_type="worker",
                    actor_id=self.worker_id,
                    resource_type="agent_job",
                    resource_id=job_id,
                    details={"attempt": lease.attempt_number},
                )
            attempt_id = session.scalar(
                select(ExecutionAttempt.id).where(
                    ExecutionAttempt.job_id == job_id,
                    ExecutionAttempt.attempt_number == lease.attempt_number,
                )
            )
            if attempt_id is not None:
                ExecutionAttemptRepository(session).complete(
                    attempt_id, target.value, result.exit_code
                )
            leases.release(job_id, self.worker_id, token)
            session.commit()

    def reap(self) -> int:
        recovered = 0
        with self.session_factory() as session:
            leases = LeaseRepository(session)
            for lease in leases.expired():
                record = AgentJobRepository(session).get(lease.job_id)
                if record is None or record.status is not AgentJobStatus.RUNNING:
                    continue
                jobs = AgentJobRepository(session)
                target = (
                    AgentJobStatus.FAILED
                    if self.config.recovery_policy == "fail"
                    else AgentJobStatus.QUEUED
                )
                try:
                    record = jobs.transition(
                        lease.job_id,
                        target,
                        expected_version=record.version,
                        failure_reason="worker lease expired",
                    )
                except InvalidStateTransitionError:
                    continue
                record.recovery_reason = "worker lease expired"
                record.prior_worker_id = lease.worker_id
                record.prior_lease_identifier = lease.lease_identifier
                record.recovery_at = utc_now()
                record.recovery_count += 1
                AuditEventRepository(session).append(
                    event_type="execution.recovered",
                    actor_type="reaper",
                    actor_id=self.worker_id,
                    resource_type="agent_job",
                    resource_id=record.id,
                    details={
                        "policy": self.config.recovery_policy,
                        "prior_worker_id": lease.worker_id,
                    },
                )
                session.delete(lease)
                recovered += 1
            session.commit()
        return recovered
