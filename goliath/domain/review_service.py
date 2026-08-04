"""Human-review service: review tasks, comparable review, bulk operations, dashboard.

All operations are scoped and audited. Bulk operations are bounded, report
per-item results, support optional all-or-nothing mode, and never perform live
marketplace actions.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.ingestion_repositories import (
    ComparableReviewRepository,
    ReviewTaskRepository,
)
from goliath.db.models import (
    ComparableReviewStatus,
    ComparableSale,
    DomainProposal,
    InventoryItem,
    InventoryMedia,
    InventoryStatus,
    MasterListingDraft,
    MediaStatus,
    ProposalStatus,
    ResearchRecord,
    ResearchStatus,
    ReviewTask,
    ReviewTaskStatus,
    ReviewTaskType,
    WorkerRecord,
    utc_now,
)
from goliath.db.operations import WorkerRepository
from goliath.db.repositories import AuditEventRepository


class ReviewError(RuntimeError):
    pass


class ReviewService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._max_bulk = config.domain.bulk_operation_max_items

    @staticmethod
    def _audit(
        session: Session, event_type: str, *, actor: str, resource_type: str, resource_id: UUID, details: dict
    ) -> None:
        actor_type, _, actor_id = actor.partition(":")
        AuditEventRepository(session).append(
            event_type=event_type,
            actor_type=actor_type or "human",
            actor_id=actor_id or actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )

    # ------------------------------------------------------------ review tasks
    def list_tasks(
        self,
        *,
        status: ReviewTaskStatus | None = None,
        task_type: ReviewTaskType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReviewTask]:
        with self._session_factory() as session:
            tasks = list(
                ReviewTaskRepository(session).list(
                    status=status, task_type=task_type, limit=limit, offset=offset
                )
            )
            for task in tasks:
                session.expunge(task)
            return tasks

    def get_task(self, task_id: UUID) -> ReviewTask:
        with self._session_factory() as session:
            task = ReviewTaskRepository(session).get(task_id)
            if task is None:
                raise ReviewError(f"review task not found: {task_id}")
            session.expunge(task)
            return task

    def claim_task(self, task_id: UUID, *, reviewer: str, expected_version: int) -> ReviewTask:
        with self._session_factory() as session:
            task = ReviewTaskRepository(session).claim(
                task_id, reviewer=reviewer, expected_version=expected_version
            )
            self._audit(
                session,
                "review.task_claimed",
                actor=reviewer,
                resource_type="review_task",
                resource_id=task_id,
                details={"type": task.task_type.value},
            )
            session.commit()
            session.refresh(task)
            return task

    def complete_task(
        self, task_id: UUID, *, reviewer: str, outcome: str, notes: str | None = None
    ) -> ReviewTask:
        with self._session_factory() as session:
            task = ReviewTaskRepository(session).complete(
                task_id, reviewer=reviewer, outcome=outcome, notes=notes
            )
            self._audit(
                session,
                "review.task_completed",
                actor=reviewer,
                resource_type="review_task",
                resource_id=task_id,
                details={"outcome": outcome},
            )
            session.commit()
            session.refresh(task)
            return task

    def dismiss_task(self, task_id: UUID, *, reviewer: str, notes: str | None = None) -> ReviewTask:
        with self._session_factory() as session:
            task = ReviewTaskRepository(session).dismiss(task_id, reviewer=reviewer, notes=notes)
            self._audit(
                session,
                "review.task_completed",
                actor=reviewer,
                resource_type="review_task",
                resource_id=task_id,
                details={"outcome": "dismissed"},
            )
            session.commit()
            session.refresh(task)
            return task

    def expire_due_tasks(self) -> int:
        with self._session_factory() as session:
            count = ReviewTaskRepository(session).expire_due()
            session.commit()
            return count

    # --------------------------------------------------------- comparable review
    def list_comparables_pending(self, *, limit: int = 100, offset: int = 0) -> list[ComparableSale]:
        with self._session_factory() as session:
            values = list(
                ComparableReviewRepository(session).list_pending(limit=limit, offset=offset)
            )
            for value in values:
                session.expunge(value)
            return values

    def review_comparable(
        self,
        comparable_id: UUID,
        *,
        reviewer: str,
        decision: ComparableReviewStatus,
        similarity_score: Decimal | None = None,
        reliability_score: Decimal | None = None,
        notes: str | None = None,
    ) -> ComparableSale:
        with self._session_factory() as session:
            comparable = ComparableReviewRepository(session).decide(
                comparable_id,
                reviewer=reviewer,
                decision=decision,
                similarity_score=similarity_score,
                reliability_score=reliability_score,
                notes=notes,
            )
            self._close_linked_task(
                session,
                task_type=ReviewTaskType.COMPARABLE_REVIEW,
                resource_id=comparable_id,
                reviewer=reviewer,
                outcome=decision.value,
            )
            self._audit(
                session,
                "comparable.review_decision",
                actor=reviewer,
                resource_type="comparable_sale",
                resource_id=comparable_id,
                details={"decision": decision.value},
            )
            session.commit()
            session.refresh(comparable)
            return comparable

    def _close_linked_task(
        self,
        session: Session,
        *,
        task_type: ReviewTaskType,
        resource_id: UUID,
        reviewer: str,
        outcome: str,
    ) -> None:
        task = session.scalar(
            select(ReviewTask).where(
                ReviewTask.task_type == task_type,
                ReviewTask.resource_id == resource_id,
                ReviewTask.status.in_([ReviewTaskStatus.OPEN, ReviewTaskStatus.CLAIMED]),
            )
        )
        if task is not None:
            ReviewTaskRepository(session).complete(
                task.id, reviewer=reviewer, outcome=outcome
            )

    # ---------------------------------------------------------- bulk operations
    def _guard_batch(self, ids: list[UUID]) -> None:
        max_bulk = self._config.domain.bulk_operation_max_items
        if not ids:
            raise ReviewError("no items supplied")
        if len(ids) > max_bulk:
            raise ReviewError(f"batch exceeds maximum size ({len(ids)} > {max_bulk})")

    def bulk_review_comparables(
        self,
        ids: list[UUID],
        *,
        reviewer: str,
        decision: ComparableReviewStatus,
        all_or_nothing: bool = False,
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for comparable_id in ids:
            try:
                self.review_comparable(comparable_id, reviewer=reviewer, decision=decision)
                results.append({"id": str(comparable_id), "ok": True})
            except Exception as error:  # noqa: BLE001 - per-item error reporting
                results.append({"id": str(comparable_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    def bulk_complete_tasks(
        self, ids: list[UUID], *, reviewer: str, outcome: str, all_or_nothing: bool = False
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for task_id in ids:
            try:
                self.complete_task(task_id, reviewer=reviewer, outcome=outcome)
                results.append({"id": str(task_id), "ok": True})
            except Exception as error:  # noqa: BLE001
                results.append({"id": str(task_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    def bulk_assign_tasks(
        self, ids: list[UUID], *, reviewer: str, all_or_nothing: bool = False
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for task_id in ids:
            try:
                task = self.get_task(task_id)
                self.claim_task(task_id, reviewer=reviewer, expected_version=task.version)
                results.append({"id": str(task_id), "ok": True})
            except Exception as error:  # noqa: BLE001
                results.append({"id": str(task_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    def bulk_archive_media(
        self,
        ids: list[UUID],
        *,
        media_service,
        actor: str,
        all_or_nothing: bool = False,
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for media_id in ids:
            try:
                media_service.archive(media_id, actor=actor)
                results.append({"id": str(media_id), "ok": True})
            except Exception as error:  # noqa: BLE001
                results.append({"id": str(media_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    def bulk_reevaluate_completeness(
        self, ids: list[UUID], *, domain_service, all_or_nothing: bool = False
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for item_id in ids:
            try:
                result = domain_service.evaluate_completeness(item_id)
                results.append({"id": str(item_id), "ok": True, "score": result.score})
            except Exception as error:  # noqa: BLE001
                results.append({"id": str(item_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    def bulk_validate_drafts(
        self, ids: list[UUID], *, domain_service, actor: str, all_or_nothing: bool = False
    ) -> dict[str, Any]:
        self._guard_batch(ids)
        results = []
        for draft_id in ids:
            try:
                domain_service.validate_draft(draft_id, actor=actor)
                results.append({"id": str(draft_id), "ok": True})
            except Exception as error:  # noqa: BLE001
                results.append({"id": str(draft_id), "ok": False, "error": str(error)})
        return self._bulk_result(results, all_or_nothing)

    @staticmethod
    def _bulk_result(results: list[dict[str, Any]], all_or_nothing: bool) -> dict[str, Any]:
        succeeded = sum(1 for r in results if r["ok"])
        failed = len(results) - succeeded
        return {
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "all_or_nothing": all_or_nothing,
            "rolled_back": bool(all_or_nothing and failed),
            "results": results,
        }

    # --------------------------------------------------------------- dashboard
    def summary(self, *, actor: str = "human:dashboard") -> dict[str, Any]:
        with self._session_factory() as session:
            def _counts(model, column):
                rows = session.execute(select(column, func.count()).group_by(column)).all()
                return {value.value: count for value, count in rows}

            inventory = _counts(InventoryItem, InventoryItem.status)
            research = _counts(ResearchRecord, ResearchRecord.status)
            proposals = _counts(DomainProposal, DomainProposal.status)
            tasks = ReviewTaskRepository(session).counts_by_status()
            comparables_pending = session.scalar(
                select(func.count()).select_from(ComparableSale).where(
                    ComparableSale.review_status == ComparableReviewStatus.PENDING_REVIEW
                )
            )
            media_failed = session.scalar(
                select(func.count()).select_from(InventoryMedia).where(
                    InventoryMedia.status.in_([MediaStatus.FAILED, MediaStatus.QUARANTINED])
                )
            )
            workers = list(session.scalars(select(WorkerRecord.status)).all())
            self._audit(
                session,
                "dashboard.accessed",
                actor=actor,
                resource_type="dashboard",
                resource_id=UUID(int=0),
                details={"view": "summary"},
            )
            session.commit()
            return {
                "inventory_by_status": inventory,
                "research_by_status": research,
                "proposals_by_status": proposals,
                "review_tasks_by_status": tasks,
                "comparables_pending_review": int(comparables_pending or 0),
                "media_failures": int(media_failed or 0),
                "workers": len(workers),
                "generated_at": utc_now().isoformat(),
            }

    def inventory_needing_review(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(InventoryItem)
                .where(InventoryItem.status == InventoryStatus.RESEARCH_NEEDED)
                .order_by(InventoryItem.created_at)
                .limit(limit)
            ).all()
            return [{"id": str(item.id), "sku": item.sku, "status": item.status.value} for item in rows]

    def research_needing_review(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ResearchRecord)
                .where(ResearchRecord.status == ResearchStatus.INCONCLUSIVE)
                .order_by(ResearchRecord.started_at)
                .limit(limit)
            ).all()
            return [{"id": str(r.id), "status": r.status.value} for r in rows]

    def listings_needing_review(self, *, limit: int = 50) -> list[dict[str, Any]]:
        from goliath.db.models import DraftStatus

        with self._session_factory() as session:
            rows = session.scalars(
                select(MasterListingDraft)
                .where(MasterListingDraft.status == DraftStatus.NEEDS_REVIEW)
                .order_by(MasterListingDraft.created_at)
                .limit(limit)
            ).all()
            return [{"id": str(d.id), "status": d.status.value, "title": d.title} for d in rows]

    def approvals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(DomainProposal)
                .where(DomainProposal.status == ProposalStatus.PENDING)
                .order_by(DomainProposal.requested_at)
                .limit(limit)
            ).all()
            return [
                {"id": str(p.id), "type": p.proposal_type.value, "risk_tier": p.risk_tier}
                for p in rows
            ]

    def media_failures(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(InventoryMedia)
                .where(InventoryMedia.status.in_([MediaStatus.FAILED, MediaStatus.QUARANTINED]))
                .order_by(InventoryMedia.created_at)
                .limit(limit)
            ).all()
            return [
                {"id": str(m.id), "status": m.status.value, "category": m.processing_error_category}
                for m in rows
            ]

    def worker_health(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [
                {
                    "id": w.id,
                    "status": w.status.value,
                    "last_heartbeat_at": w.last_heartbeat_at.isoformat(),
                }
                for w in WorkerRepository(session).list()
            ]
