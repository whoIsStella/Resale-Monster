from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from goliath.db.models import (
    AgentJobRecord,
    AgentJobStatus,
    ApiKey,
    ApiPrincipal,
    ExecutionAttempt,
    IdempotencyRecord,
    JobLease,
    Schedule,
    WorkerRecord,
    WorkerStatus,
    utc_now,
)
from goliath.db.repositories import InvalidStateTransitionError, RecordNotFoundError


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class WorkerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register(
        self, worker_id: str, hostname: str, process_id: int, concurrency: int, version: str
    ) -> WorkerRecord:
        worker = WorkerRecord(
            id=worker_id,
            hostname=hostname,
            process_id=process_id,
            configured_concurrency=concurrency,
            software_version=version,
            status=WorkerStatus.STARTING,
        )
        self.session.add(worker)
        self.session.flush()
        return worker

    def heartbeat(self, worker_id: str, active_job_count: int = 0) -> WorkerRecord:
        worker = self.session.get(WorkerRecord, worker_id)
        if worker is None:
            raise RecordNotFoundError(f"worker not found: {worker_id}")
        worker.last_heartbeat_at = utc_now()
        worker.active_job_count = active_job_count
        worker.status = WorkerStatus.HEALTHY
        self.session.flush()
        return worker

    def set_status(self, worker_id: str, status: WorkerStatus) -> WorkerRecord:
        worker = self.session.get(WorkerRecord, worker_id)
        if worker is None:
            raise RecordNotFoundError(f"worker not found: {worker_id}")
        worker.status = status
        if status is WorkerStatus.STOPPED:
            worker.shutdown_at = utc_now()
        self.session.flush()
        return worker

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self.session.get(WorkerRecord, worker_id)

    def list(self) -> list[WorkerRecord]:
        return list(self.session.scalars(select(WorkerRecord).order_by(WorkerRecord.id)).all())

    def mark_stale(self, before: datetime) -> list[WorkerRecord]:
        workers = list(
            self.session.scalars(
                select(WorkerRecord).where(
                    WorkerRecord.last_heartbeat_at < before,
                    WorkerRecord.status.in_(
                        [WorkerStatus.STARTING, WorkerStatus.HEALTHY, WorkerStatus.DRAINING]
                    ),
                )
            ).all()
        )
        for worker in workers:
            worker.status = WorkerStatus.STALE
        self.session.flush()
        return workers


class LeaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def acquire(
        self, worker_id: str, lease_seconds: float, now: datetime | None = None
    ) -> tuple[AgentJobRecord, JobLease, str] | None:
        now = now or utc_now()
        candidate = self.session.scalar(
            select(AgentJobRecord)
            .where(
                AgentJobRecord.status == AgentJobStatus.QUEUED,
                (AgentJobRecord.next_eligible_at.is_(None))
                | (AgentJobRecord.next_eligible_at <= now),
            )
            .order_by(AgentJobRecord.queued_at, AgentJobRecord.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        result = self.session.execute(
            update(AgentJobRecord)
            .where(
                AgentJobRecord.id == candidate.id,
                AgentJobRecord.version == candidate.version,
                AgentJobRecord.status == AgentJobStatus.QUEUED,
            )
            .values(status=AgentJobStatus.RUNNING, version=candidate.version + 1, started_at=now)
        )
        if result.rowcount != 1:
            return None
        token = secrets.token_urlsafe(32)
        lease_id = uuid4().hex
        previous_attempt = (
            self.session.scalar(
                select(func.max(ExecutionAttempt.attempt_number)).where(
                    ExecutionAttempt.job_id == candidate.id
                )
            )
            or 0
        )
        lease = JobLease(
            job_id=candidate.id,
            worker_id=worker_id,
            lease_identifier=lease_id,
            lease_token_hash=hash_secret(token),
            acquired_at=now,
            expires_at=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
            attempt_number=previous_attempt + 1,
        )
        self.session.add(lease)
        self.session.flush()
        self.session.expire(candidate)
        return candidate, lease, token

    def get(self, job_id: UUID) -> JobLease | None:
        return self.session.get(JobLease, job_id)

    def heartbeat(
        self,
        job_id: UUID,
        worker_id: str,
        token: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> JobLease:
        now = now or utc_now()
        lease = self.get(job_id)
        if (
            lease is None
            or lease.worker_id != worker_id
            or not hmac.compare_digest(lease.lease_token_hash, hash_secret(token))
            or _utc(lease.expires_at) <= now
        ):
            raise InvalidStateTransitionError("stale or invalid lease token")
        lease.last_heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        self.session.flush()
        return lease

    def validate(
        self, job_id: UUID, worker_id: str, token: str, now: datetime | None = None
    ) -> JobLease:
        now = now or utc_now()
        lease = self.get(job_id)
        if (
            lease is None
            or lease.worker_id != worker_id
            or not hmac.compare_digest(lease.lease_token_hash, hash_secret(token))
            or _utc(lease.expires_at) <= now
        ):
            raise InvalidStateTransitionError("stale or invalid lease token")
        return lease

    def release(self, job_id: UUID, worker_id: str, token: str) -> None:
        self.validate(job_id, worker_id, token)
        self.session.execute(delete(JobLease).where(JobLease.job_id == job_id))
        self.session.flush()

    def expired(self, now: datetime | None = None) -> list[JobLease]:
        return list(
            self.session.scalars(
                select(JobLease)
                .where(JobLease.expires_at <= (now or utc_now()))
                .order_by(JobLease.expires_at)
            ).all()
        )


class ExecutionAttemptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, job_id: UUID, worker_id: str, lease_identifier: str, attempt_number: int
    ) -> ExecutionAttempt:
        attempt = ExecutionAttempt(
            job_id=job_id,
            worker_id=worker_id,
            lease_identifier=lease_identifier,
            attempt_number=attempt_number,
            started_at=utc_now(),
            status="running",
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def complete(
        self,
        attempt_id: UUID,
        status: str,
        exit_code: int | None = None,
        failure_category: str | None = None,
    ) -> ExecutionAttempt:
        attempt = self.session.get(ExecutionAttempt, attempt_id)
        if attempt is None:
            raise RecordNotFoundError(f"attempt not found: {attempt_id}")
        attempt.status = status
        attempt.exit_code = exit_code
        attempt.failure_category = failure_category
        attempt.completed_at = utc_now()
        self.session.flush()
        return attempt


class ScheduleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **values: Any) -> Schedule:
        schedule = Schedule(**values)
        self.session.add(schedule)
        self.session.flush()
        return schedule

    def get(self, schedule_id: UUID) -> Schedule | None:
        return self.session.get(Schedule, schedule_id)

    def get_by_name(self, name: str) -> Schedule | None:
        return self.session.scalar(select(Schedule).where(Schedule.name == name))

    def list(self, enabled: bool | None = None) -> list[Schedule]:
        statement = select(Schedule).order_by(Schedule.name)
        if enabled is not None:
            statement = statement.where(Schedule.enabled == enabled)
        return list(self.session.scalars(statement).all())

    def set_enabled(self, schedule_id: UUID, enabled: bool) -> Schedule:
        schedule = self.get(schedule_id)
        if schedule is None:
            raise RecordNotFoundError(f"schedule not found: {schedule_id}")
        schedule.enabled = enabled
        schedule.updated_at = utc_now()
        self.session.flush()
        return schedule


class AuthRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_key(
        self, principal_name: str, scopes: list[str], expires_at: datetime | None = None
    ) -> tuple[ApiPrincipal, ApiKey, str]:
        principal = self.session.scalar(
            select(ApiPrincipal).where(ApiPrincipal.name == principal_name)
        )
        if principal is None:
            principal = ApiPrincipal(name=principal_name)
            self.session.add(principal)
            self.session.flush()
        raw = "rg_" + secrets.token_urlsafe(32)
        key = ApiKey(
            principal_id=principal.id,
            key_prefix=raw[:10],
            key_hash=hash_secret(raw),
            scopes=scopes,
            expires_at=expires_at,
        )
        self.session.add(key)
        self.session.flush()
        return principal, key, raw

    def authenticate(
        self, raw_key: str, now: datetime | None = None
    ) -> tuple[ApiPrincipal, ApiKey] | None:
        now = now or utc_now()
        key = self.session.scalar(
            select(ApiKey).where(
                ApiKey.key_hash == hash_secret(raw_key), ApiKey.revoked_at.is_(None)
            )
        )
        if key is None or (key.expires_at is not None and _utc(key.expires_at) <= _utc(now)):
            return None
        principal = self.session.get(ApiPrincipal, key.principal_id)
        if principal is None or principal.revoked_at is not None:
            return None
        key.last_used_at = now
        self.session.flush()
        return principal, key

    def revoke(self, key_id: UUID) -> None:
        key = self.session.get(ApiKey, key_id)
        if key is None:
            raise RecordNotFoundError(f"API key not found: {key_id}")
        key.revoked_at = utc_now()
        self.session.flush()

    def list_keys(self) -> list[ApiKey]:
        return list(self.session.scalars(select(ApiKey).order_by(ApiKey.created_at)).all())


class IdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, principal_id: UUID, key: str, now: datetime | None = None
    ) -> IdempotencyRecord | None:
        record = self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.principal_id == principal_id,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if record is not None and _utc(record.expires_at) <= (now or utc_now()):
            self.session.delete(record)
            self.session.flush()
            return None
        return record

    def create(
        self,
        principal_id: UUID,
        key: str,
        request_hash: str,
        resource_type: str,
        resource_id: UUID,
        expires_at: datetime,
    ) -> IdempotencyRecord:
        record = IdempotencyRecord(
            principal_id=principal_id,
            idempotency_key=key,
            request_hash=request_hash,
            resource_type=resource_type,
            resource_id=resource_id,
            expires_at=expires_at,
        )
        self.session.add(record)
        self.session.flush()
        return record
