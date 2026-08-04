"""Bounded repositories for milestone-five media, comparable, and review entities.

Each repository exposes only domain-specific methods — never raw sessions,
arbitrary SQL, unrestricted filters, storage credentials, filesystem handles, or
generic model access.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from goliath.db.models import (
    ComparableImport,
    ComparableImportRow,
    ComparableReviewDecision,
    ComparableReviewStatus,
    ComparableSale,
    ImageQualityResult,
    ImportStatus,
    MediaDerivation,
    MediaJobStatus,
    MediaProcessingAttempt,
    MediaProcessingJob,
    PerceptualHash,
    PricingSourcePolicyRecord,
    ReviewTask,
    ReviewTaskEvent,
    ReviewTaskStatus,
    ReviewTaskType,
    utc_now,
)
from goliath.db.repositories import (
    InvalidStateTransitionError,
    RecordNotFoundError,
    VersionConflictError,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class MediaDerivationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, parent_media_id: UUID, operation: str) -> bool:
        return (
            self._session.scalar(
                select(MediaDerivation.id).where(
                    MediaDerivation.parent_media_id == parent_media_id,
                    MediaDerivation.operation == operation,
                )
            )
            is not None
        )

    def add(
        self,
        *,
        parent_media_id: UUID,
        operation: str,
        storage_key: str,
        media_type: str,
        checksum: str,
        file_size: int,
        width: int | None = None,
        height: int | None = None,
    ) -> MediaDerivation:
        derivation = MediaDerivation(
            parent_media_id=parent_media_id,
            operation=operation,
            storage_key=storage_key,
            media_type=media_type,
            checksum=checksum,
            file_size=file_size,
            width=width,
            height=height,
        )
        self._session.add(derivation)
        self._session.flush()
        return derivation

    def list_for_media(self, parent_media_id: UUID) -> Sequence[MediaDerivation]:
        return self._session.scalars(
            select(MediaDerivation)
            .where(MediaDerivation.parent_media_id == parent_media_id)
            .order_by(MediaDerivation.created_at)
        ).all()


class MediaProcessingJobRepository:
    """Durable, leased image-processing jobs with retries, backoff, and recovery."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, media_id: UUID, *, max_attempts: int = 3) -> MediaProcessingJob:
        """Idempotently enqueue processing for a media item (prevents duplicate work)."""
        existing = self._session.scalar(
            select(MediaProcessingJob).where(
                MediaProcessingJob.media_id == media_id,
                MediaProcessingJob.status.in_(
                    [MediaJobStatus.QUEUED, MediaJobStatus.RUNNING, MediaJobStatus.SUCCEEDED]
                ),
            )
        )
        if existing is not None:
            return existing
        job = MediaProcessingJob(media_id=media_id, max_attempts=max_attempts)
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: UUID) -> MediaProcessingJob | None:
        return self._session.get(MediaProcessingJob, job_id)

    def list(self, *, status: MediaJobStatus | None = None) -> Sequence[MediaProcessingJob]:
        statement = select(MediaProcessingJob)
        if status is not None:
            statement = statement.where(MediaProcessingJob.status == status)
        return self._session.scalars(
            statement.order_by(MediaProcessingJob.created_at)
        ).all()

    def claim(
        self, worker_id: str, *, lease_seconds: float = 120.0, now: datetime | None = None
    ) -> tuple[MediaProcessingJob, str] | None:
        now = now or utc_now()
        candidate = self._session.scalar(
            select(MediaProcessingJob)
            .where(
                MediaProcessingJob.status == MediaJobStatus.QUEUED,
                (MediaProcessingJob.next_eligible_at.is_(None))
                | (MediaProcessingJob.next_eligible_at <= now),
            )
            .order_by(MediaProcessingJob.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        token = secrets.token_urlsafe(24)
        result = self._session.execute(
            update(MediaProcessingJob)
            .where(
                MediaProcessingJob.id == candidate.id,
                MediaProcessingJob.version == candidate.version,
                MediaProcessingJob.status == MediaJobStatus.QUEUED,
            )
            .values(
                status=MediaJobStatus.RUNNING,
                version=candidate.version + 1,
                attempts=candidate.attempts + 1,
                locked_by=worker_id,
                lease_token_hash=_hash(token),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=candidate.started_at or now,
            )
        )
        if result.rowcount != 1:
            return None
        self._session.expire(candidate)
        return candidate, token

    def complete_success(self, job_id: UUID, worker_id: str, token: str) -> MediaProcessingJob:
        job = self._validate_lease(job_id, worker_id, token)
        job.status = MediaJobStatus.SUCCEEDED
        job.completed_at = utc_now()
        job.locked_by = None
        job.lease_token_hash = None
        job.lease_expires_at = None
        job.version += 1
        self._session.flush()
        return job

    def complete_failure(
        self,
        job_id: UUID,
        worker_id: str,
        token: str,
        *,
        error_category: str,
        failure_reason: str,
        backoff_seconds: float = 5.0,
    ) -> MediaProcessingJob:
        job = self._validate_lease(job_id, worker_id, token)
        job.error_category = error_category
        job.failure_reason = failure_reason
        job.locked_by = None
        job.lease_token_hash = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.status = MediaJobStatus.FAILED
            job.completed_at = utc_now()
        else:
            job.status = MediaJobStatus.QUEUED
            job.next_eligible_at = utc_now() + timedelta(
                seconds=backoff_seconds * job.attempts
            )
        job.version += 1
        self._session.flush()
        return job

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Requeue jobs whose lease expired (crash recovery)."""
        now = now or utc_now()
        expired = self._session.scalars(
            select(MediaProcessingJob).where(
                MediaProcessingJob.status == MediaJobStatus.RUNNING,
                MediaProcessingJob.lease_expires_at.is_not(None),
                MediaProcessingJob.lease_expires_at <= now,
            )
        ).all()
        for job in expired:
            job.locked_by = None
            job.lease_token_hash = None
            job.lease_expires_at = None
            if job.attempts >= job.max_attempts:
                job.status = MediaJobStatus.FAILED
                job.completed_at = now
                job.error_category = "lease_expired"
            else:
                job.status = MediaJobStatus.QUEUED
                job.next_eligible_at = now
        self._session.flush()
        return len(expired)

    def _validate_lease(self, job_id: UUID, worker_id: str, token: str) -> MediaProcessingJob:
        job = self.get(job_id)
        if job is None:
            raise RecordNotFoundError(f"media job not found: {job_id}")
        if (
            job.locked_by != worker_id
            or job.lease_token_hash is None
            or not hmac.compare_digest(job.lease_token_hash, _hash(token))
        ):
            raise InvalidStateTransitionError("invalid or stale media job lease")
        return job


class MediaProcessingAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, job_id: UUID, worker_id: str, attempt_number: int) -> MediaProcessingAttempt:
        attempt = MediaProcessingAttempt(
            job_id=job_id,
            worker_id=worker_id,
            attempt_number=attempt_number,
            status="running",
        )
        self._session.add(attempt)
        self._session.flush()
        return attempt

    def complete(
        self, attempt_id: UUID, *, status: str, error_category: str | None = None
    ) -> MediaProcessingAttempt:
        attempt = self._session.get(MediaProcessingAttempt, attempt_id)
        if attempt is None:
            raise RecordNotFoundError(f"attempt not found: {attempt_id}")
        attempt.status = status
        attempt.error_category = error_category
        attempt.completed_at = utc_now()
        self._session.flush()
        return attempt


class ImageQualityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        media_id: UUID,
        passed: bool,
        findings: list[str],
        width: int | None = None,
        height: int | None = None,
        blur_variance: Decimal | None = None,
        aspect_ratio: Decimal | None = None,
        color_mode: str | None = None,
    ) -> ImageQualityResult:
        result = ImageQualityResult(
            media_id=media_id,
            passed=passed,
            findings=findings,
            width=width,
            height=height,
            blur_variance=blur_variance,
            aspect_ratio=aspect_ratio,
            color_mode=color_mode,
        )
        self._session.add(result)
        self._session.flush()
        return result

    def latest_for_media(self, media_id: UUID) -> ImageQualityResult | None:
        return self._session.scalar(
            select(ImageQualityResult)
            .where(ImageQualityResult.media_id == media_id)
            .order_by(ImageQualityResult.created_at.desc())
        )


class PerceptualHashRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, *, media_id: UUID, algorithm: str, hash_hex: str) -> PerceptualHash:
        record = PerceptualHash(media_id=media_id, algorithm=algorithm, hash_hex=hash_hex)
        self._session.add(record)
        self._session.flush()
        return record

    def find_duplicates(
        self, *, algorithm: str, hash_hex: str, exclude_media_id: UUID | None = None
    ) -> Sequence[PerceptualHash]:
        statement = select(PerceptualHash).where(
            PerceptualHash.algorithm == algorithm,
            PerceptualHash.hash_hex == hash_hex,
        )
        if exclude_media_id is not None:
            statement = statement.where(PerceptualHash.media_id != exclude_media_id)
        return self._session.scalars(statement).all()


class ComparableImportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        inventory_item_id: UUID,
        source_format: str,
        requested_by: str,
        dry_run: bool,
        total_rows: int,
    ) -> ComparableImport:
        record = ComparableImport(
            inventory_item_id=inventory_item_id,
            source_format=source_format,
            requested_by=requested_by,
            dry_run=dry_run,
            total_rows=total_rows,
            status=ImportStatus.DRY_RUN if dry_run else ImportStatus.PENDING,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def add_row(
        self,
        import_id: UUID,
        *,
        row_number: int,
        raw: dict[str, Any],
        status: str,
        error: str | None = None,
        comparable_id: UUID | None = None,
    ) -> ComparableImportRow:
        row = ComparableImportRow(
            import_id=import_id,
            row_number=row_number,
            raw=raw,
            status=status,
            error=error,
            comparable_id=comparable_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def finalize(
        self,
        import_id: UUID,
        *,
        status: ImportStatus,
        imported_rows: int,
        failed_rows: int,
        duplicate_rows: int,
        summary: dict[str, Any],
    ) -> ComparableImport:
        record = self._session.get(ComparableImport, import_id)
        if record is None:
            raise RecordNotFoundError(f"import not found: {import_id}")
        record.status = status
        record.imported_rows = imported_rows
        record.failed_rows = failed_rows
        record.duplicate_rows = duplicate_rows
        record.summary = summary
        self._session.flush()
        return record

    def get(self, import_id: UUID) -> ComparableImport | None:
        return self._session.get(ComparableImport, import_id)

    def list_rows(self, import_id: UUID) -> Sequence[ComparableImportRow]:
        return self._session.scalars(
            select(ComparableImportRow)
            .where(ComparableImportRow.import_id == import_id)
            .order_by(ComparableImportRow.row_number)
        ).all()


class ComparableReviewRepository:
    """Human review of acquired comparables; drives their review lifecycle."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_pending(self, *, limit: int = 100, offset: int = 0) -> Sequence[ComparableSale]:
        return self._session.scalars(
            select(ComparableSale)
            .where(ComparableSale.review_status == ComparableReviewStatus.PENDING_REVIEW)
            .order_by(ComparableSale.captured_at)
            .limit(limit)
            .offset(offset)
        ).all()

    def decide(
        self,
        comparable_id: UUID,
        *,
        reviewer: str,
        decision: ComparableReviewStatus,
        similarity_score: Decimal | None = None,
        reliability_score: Decimal | None = None,
        notes: str | None = None,
    ) -> ComparableSale:
        if decision is ComparableReviewStatus.PENDING_REVIEW:
            raise ValueError("cannot decide a comparable back to pending")
        comparable = self._session.get(ComparableSale, comparable_id)
        if comparable is None:
            raise RecordNotFoundError(f"comparable not found: {comparable_id}")
        if comparable.review_status not in {
            ComparableReviewStatus.PENDING_REVIEW,
            ComparableReviewStatus.ACCEPTED,
            ComparableReviewStatus.REJECTED,
        }:
            raise InvalidStateTransitionError(
                f"comparable is not reviewable: {comparable.review_status.value}"
            )
        comparable.review_status = decision
        comparable.reviewed_by = reviewer
        comparable.reviewed_at = utc_now()
        if similarity_score is not None:
            comparable.similarity_score = similarity_score
        if reliability_score is not None:
            comparable.reliability_score = reliability_score
        if decision is ComparableReviewStatus.INVALIDATED and comparable.invalidated_at is None:
            comparable.invalidated_at = utc_now()
            comparable.invalidation_reason = notes
        self._session.add(
            ComparableReviewDecision(
                comparable_id=comparable_id,
                reviewer=reviewer,
                decision=decision,
                similarity_score=similarity_score,
                reliability_score=reliability_score,
                notes=notes,
            )
        )
        self._session.flush()
        return comparable

    def accepted_for_item(self, inventory_item_id: UUID) -> Sequence[ComparableSale]:
        return self._session.scalars(
            select(ComparableSale).where(
                ComparableSale.inventory_item_id == inventory_item_id,
                ComparableSale.review_status == ComparableReviewStatus.ACCEPTED,
                ComparableSale.invalidated_at.is_(None),
            )
        ).all()


class PricingSourcePolicyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, name: str, version: str, reviewed_only: bool, settings: dict[str, Any]
    ) -> PricingSourcePolicyRecord:
        record = PricingSourcePolicyRecord(
            name=name, version=version, reviewed_only=reviewed_only, settings=settings
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get_active(self, name: str = "default") -> PricingSourcePolicyRecord | None:
        return self._session.scalar(
            select(PricingSourcePolicyRecord)
            .where(
                PricingSourcePolicyRecord.name == name,
                PricingSourcePolicyRecord.is_active.is_(True),
            )
            .order_by(PricingSourcePolicyRecord.created_at.desc())
        )

    def list(self) -> Sequence[PricingSourcePolicyRecord]:
        return self._session.scalars(
            select(PricingSourcePolicyRecord).order_by(PricingSourcePolicyRecord.created_at)
        ).all()


class ReviewTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_idempotent(
        self,
        *,
        task_type: ReviewTaskType,
        resource_type: str,
        resource_id: UUID,
        reason: str,
        dedupe_suffix: str = "",
        priority: int = 100,
        due_at: datetime | None = None,
    ) -> tuple[ReviewTask, bool]:
        """Create an open review task, or return the existing open one (idempotent)."""
        dedupe_key = f"{task_type.value}:{resource_id}:{dedupe_suffix}"
        existing = self._session.scalar(
            select(ReviewTask).where(ReviewTask.dedupe_key == dedupe_key)
        )
        if existing is not None and existing.status in {
            ReviewTaskStatus.OPEN,
            ReviewTaskStatus.CLAIMED,
        }:
            return existing, False
        if existing is not None:
            # A prior task for this dedupe key was closed; reopen a fresh row by
            # rotating the key so the unique constraint is preserved.
            dedupe_key = f"{dedupe_key}:{secrets.token_hex(4)}"
        task = ReviewTask(
            task_type=task_type,
            resource_type=resource_type,
            resource_id=resource_id,
            dedupe_key=dedupe_key,
            reason=reason,
            priority=priority,
            due_at=due_at,
            status=ReviewTaskStatus.OPEN,
        )
        self._session.add(task)
        self._session.flush()
        self._event(task.id, "created", "system", {"reason": reason})
        return task, True

    def get(self, task_id: UUID) -> ReviewTask | None:
        return self._session.get(ReviewTask, task_id)

    def list(
        self,
        *,
        status: ReviewTaskStatus | None = None,
        task_type: ReviewTaskType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ReviewTask]:
        statement = select(ReviewTask)
        if status is not None:
            statement = statement.where(ReviewTask.status == status)
        if task_type is not None:
            statement = statement.where(ReviewTask.task_type == task_type)
        return self._session.scalars(
            statement.order_by(ReviewTask.priority.desc(), ReviewTask.created_at)
            .limit(limit)
            .offset(offset)
        ).all()

    def claim(self, task_id: UUID, *, reviewer: str, expected_version: int) -> ReviewTask:
        """Atomically claim a task; a competing claim on the same version fails."""
        task = self.get(task_id)
        if task is None:
            raise RecordNotFoundError(f"review task not found: {task_id}")
        # Pure compare-and-swap on (id, version, status=open): a competing claim
        # that already advanced the version/status loses the race here.
        result = self._session.execute(
            update(ReviewTask)
            .where(
                ReviewTask.id == task_id,
                ReviewTask.version == expected_version,
                ReviewTask.status == ReviewTaskStatus.OPEN,
            )
            .values(
                status=ReviewTaskStatus.CLAIMED,
                assigned_reviewer=reviewer,
                claimed_at=utc_now(),
                version=expected_version + 1,
            )
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise VersionConflictError("review task was already claimed or is not open")
        self._session.expire(task)
        self._event(task_id, "claimed", reviewer, {})
        return task

    def complete(
        self,
        task_id: UUID,
        *,
        reviewer: str,
        outcome: str,
        notes: str | None = None,
        expected_version: int | None = None,
    ) -> ReviewTask:
        return self._close(
            task_id,
            reviewer=reviewer,
            status=ReviewTaskStatus.COMPLETED,
            outcome=outcome,
            notes=notes,
            expected_version=expected_version,
        )

    def dismiss(
        self, task_id: UUID, *, reviewer: str, notes: str | None = None
    ) -> ReviewTask:
        return self._close(
            task_id,
            reviewer=reviewer,
            status=ReviewTaskStatus.DISMISSED,
            outcome="dismissed",
            notes=notes,
        )

    def expire_due(self, *, now: datetime | None = None) -> int:
        now = now or utc_now()
        due = self._session.scalars(
            select(ReviewTask).where(
                ReviewTask.status.in_([ReviewTaskStatus.OPEN, ReviewTaskStatus.CLAIMED]),
                ReviewTask.due_at.is_not(None),
                ReviewTask.due_at <= now,
            )
        ).all()
        for task in due:
            task.status = ReviewTaskStatus.EXPIRED
            task.version += 1
            self._event(task.id, "expired", "system", {})
        self._session.flush()
        return len(due)

    def counts_by_status(self) -> dict[str, int]:
        rows = self._session.execute(
            select(ReviewTask.status, func.count()).group_by(ReviewTask.status)
        ).all()
        return {status.value: count for status, count in rows}

    def _close(
        self,
        task_id: UUID,
        *,
        reviewer: str,
        status: ReviewTaskStatus,
        outcome: str,
        notes: str | None,
        expected_version: int | None = None,
    ) -> ReviewTask:
        task = self.get(task_id)
        if task is None:
            raise RecordNotFoundError(f"review task not found: {task_id}")
        if task.status in {
            ReviewTaskStatus.COMPLETED,
            ReviewTaskStatus.DISMISSED,
            ReviewTaskStatus.EXPIRED,
        }:
            raise InvalidStateTransitionError("review task is already closed")
        if expected_version is not None and task.version != expected_version:
            raise VersionConflictError("review task version changed")
        task.status = status
        task.outcome = outcome
        task.notes = notes
        task.assigned_reviewer = task.assigned_reviewer or reviewer
        task.completed_at = utc_now()
        task.version += 1
        self._session.flush()
        self._event(task_id, status.value, reviewer, {"outcome": outcome})
        return task

    def _event(self, task_id: UUID, event_type: str, actor: str, detail: dict[str, Any]) -> None:
        self._session.add(
            ReviewTaskEvent(
                task_id=task_id, event_type=event_type, actor=actor, detail=detail
            )
        )
        self._session.flush()

    def list_events(self, task_id: UUID) -> Sequence[ReviewTaskEvent]:
        return self._session.scalars(
            select(ReviewTaskEvent)
            .where(ReviewTaskEvent.task_id == task_id)
            .order_by(ReviewTaskEvent.created_at)
        ).all()
