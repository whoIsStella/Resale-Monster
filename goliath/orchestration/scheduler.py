from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from goliath.core.schemas import REQUIRED_FORBIDDEN_ACTIONS, JobSubmission
from goliath.db.models import AgentJobStatus, Schedule, utc_now
from goliath.db.operations import ScheduleRepository

SAFE_SCHEDULE_TASKS = {"code_change", "test", "review", "research", "analysis", "operations"}
MARKETPLACE_TASK_TYPE = "marketplace_operation"
MARKETPLACE_WORKER_NAME = "marketplace-automation"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MarketplaceWorkflow:
    operation: str
    schedule_setting: str
    capability: str | None
    write_classification: str


MARKETPLACE_WORKFLOWS: tuple[MarketplaceWorkflow, ...] = (
    MarketplaceWorkflow("account_sync", "account_sync", "sync_account", "read_only"),
    MarketplaceWorkflow("listing_state_sync", "listing_sync", "read_listings", "read_only"),
    MarketplaceWorkflow(
        "listing_reconciliation", "listing_reconciliation", "read_listing", "read_only"
    ),
    MarketplaceWorkflow("order_sync", "order_sync", "read_orders", "write_capable"),
    MarketplaceWorkflow("paid_sale_detection", "order_sync", None, "write_capable"),
    MarketplaceWorkflow("inventory_reservation", "order_sync", None, "write_capable"),
    MarketplaceWorkflow(
        "cross_marketplace_delisting", "order_sync", "end_listing", "write_capable"
    ),
    MarketplaceWorkflow("offer_sync", "offer_sync", "read_offers", "read_only"),
    MarketplaceWorkflow("offer_processing", "offer_sync", None, "write_capable"),
    MarketplaceWorkflow("message_sync", "message_sync", "read_messages", "read_only"),
    MarketplaceWorkflow(
        "routine_message_response", "message_sync", "send_message", "write_capable"
    ),
    MarketplaceWorkflow("price_automation", "pricing", "update_listing", "write_capable"),
    MarketplaceWorkflow("listing_refresh", "refresh", "refresh_listing", "write_capable"),
    MarketplaceWorkflow("listing_share_or_promotion", "promotion_share", None, "write_capable"),
    MarketplaceWorkflow("stale_inventory", "stale_inventory", None, "write_capable"),
    MarketplaceWorkflow("shipping_task_generation", "shipping", None, "write_capable"),
    MarketplaceWorkflow("shipping_label_purchase", "shipping", "purchase_label", "write_capable"),
    MarketplaceWorkflow("tracking_sync", "tracking", "update_tracking", "write_capable"),
    MarketplaceWorkflow("financial_reconciliation", "financial_reconciliation", None, "read_only"),
    MarketplaceWorkflow(
        "expired_reservation_release", "reservation_cleanup", None, "write_capable"
    ),
    MarketplaceWorkflow(
        "circuit_breaker_probe", "circuit_breaker_probe", "health_check", "read_only"
    ),
)


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    zone = ZoneInfo(schedule.timezone)
    point = after.astimezone(zone)
    if schedule.timing_type == "once":
        parsed = datetime.fromisoformat(schedule.timing_value)
        value = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
        return value.astimezone(UTC) if value > point else None
    if schedule.timing_type == "interval":
        return (point + timedelta(seconds=float(schedule.timing_value))).astimezone(UTC)
    if schedule.timing_value == "@hourly":
        candidate = point.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif schedule.timing_value == "@daily":
        candidate = point.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif schedule.timing_value.startswith("*/") and schedule.timing_value.endswith(" * * * *"):
        minutes = int(schedule.timing_value.split("/")[1].split()[0])
        candidate = point.replace(second=0, microsecond=0) + timedelta(
            minutes=minutes - point.minute % minutes
        )
    else:
        raise ValueError("unsupported cron expression")
    return candidate.astimezone(UTC)


class SchedulerService:
    def __init__(self, *, uow_factory, orchestration_service, config) -> None:
        self.uow_factory = uow_factory
        self.orchestration_service = orchestration_service
        self.config = config

    def create(
        self,
        *,
        name: str,
        task_type: str,
        objective_template: str,
        workspace: Path,
        timing_type: str,
        timing_value: str,
        owner: str,
        requested_agent: str | None = None,
        permissions: list[str] | None = None,
        context_files: list[str] | None = None,
        timezone: str | None = None,
    ) -> Schedule:
        if task_type not in SAFE_SCHEDULE_TASKS:
            raise ValueError("schedule task type is not allowed")
        workspace = self.config.validate_workspace(workspace)
        now = utc_now()
        schedule = Schedule(
            name=name,
            task_type=task_type,
            objective_template=objective_template,
            requested_agent=requested_agent,
            workspace_path=str(workspace),
            permissions=permissions or ["read_files"],
            context_files=context_files or [],
            schedule_metadata={},
            timing_type=timing_type,
            timing_value=timing_value,
            timezone=timezone or self.config.scheduler_timezone,
            missed_run_policy=self.config.missed_run_policy,
            max_catch_up_runs=self.config.max_catch_up_runs,
            owner=owner,
            enabled=True,
            next_run_at=next_run(
                Schedule(
                    timing_type=timing_type,
                    timing_value=timing_value,
                    timezone=timezone or self.config.scheduler_timezone,
                ),
                now,
            ),
            created_at=now,
            updated_at=now,
        )
        with self.uow_factory() as uow:
            uow.session.add(schedule)
            uow.session.flush()
            return schedule

    def install_marketplace_schedules(
        self, *, account_ids: set[UUID] | None = None, now: datetime | None = None
    ) -> dict[str, object]:
        """Install one idempotent persisted schedule per account and workflow."""
        from goliath.db.marketplace_repositories import MarketplaceAccountRepository

        now = now or utc_now()
        created = 0
        existing = 0
        schedule_ids: list[str] = []
        workspace = self.config.workspace_roots[0].resolve()
        with self.uow_factory() as uow:
            schedules = ScheduleRepository(uow.session)
            accounts = MarketplaceAccountRepository(uow.session).list()
            for account in accounts:
                if account_ids is not None and account.id not in account_ids:
                    continue
                for workflow in MARKETPLACE_WORKFLOWS:
                    setting = getattr(self.config.marketplace.schedules, workflow.schedule_setting)
                    name = f"marketplace:{account.id}:{workflow.operation}"
                    schedule = schedules.get_by_name(name)
                    if schedule is not None:
                        existing += 1
                        schedule_ids.append(str(schedule.id))
                        continue
                    metadata = {
                        "marketplace_account_id": str(account.id),
                        "marketplace": account.marketplace.value,
                        "operation_type": workflow.operation,
                        "requested_capability": workflow.capability,
                        "write_classification": workflow.write_classification,
                        "policy_version": "marketplace-scheduler-v1",
                    }
                    schedule = schedules.create(
                        name=name,
                        task_type=MARKETPLACE_TASK_TYPE,
                        objective_template=f"Run durable marketplace workflow: {workflow.operation}",
                        requested_agent=MARKETPLACE_WORKER_NAME,
                        workspace_path=str(workspace),
                        permissions=[],
                        context_files=[],
                        schedule_metadata=metadata,
                        timing_type="interval",
                        timing_value=str(setting.interval_seconds),
                        timezone=self.config.scheduler_timezone,
                        missed_run_policy="skip",
                        max_catch_up_runs=1,
                        owner="system:marketplace-scheduler",
                        enabled=setting.enabled,
                        next_run_at=now + timedelta(seconds=setting.interval_seconds),
                        created_at=now,
                        updated_at=now,
                    )
                    uow.audits.append(
                        event_type="marketplace.schedule.installed",
                        actor_type="system",
                        actor_id="marketplace-scheduler",
                        resource_type="schedule",
                        resource_id=schedule.id,
                        details={"operation": workflow.operation, "account_id": str(account.id)},
                    )
                    created += 1
                    schedule_ids.append(str(schedule.id))
        return {"created": created, "existing": existing, "schedule_ids": schedule_ids}

    def run_now(self, schedule_id: UUID, *, now: datetime | None = None):
        now = now or utc_now()
        with self.uow_factory() as uow:
            schedule = ScheduleRepository(uow.session).get(schedule_id)
            if schedule is None:
                from goliath.db.repositories import RecordNotFoundError

                raise RecordNotFoundError(f"schedule not found: {schedule_id}")
            if schedule.task_type == MARKETPLACE_TASK_TYPE:
                return self._enqueue_marketplace(uow, schedule, now)
        raise ValueError("run_now is only supported here for marketplace schedules")

    def tick(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        created = 0
        pending: list[JobSubmission] = []
        with self.uow_factory() as uow:
            schedules = ScheduleRepository(uow.session).list(enabled=True)
            for schedule in schedules:
                if schedule.next_run_at is None or _utc(schedule.next_run_at) > _utc(now):
                    continue
                scheduled_for = _utc(schedule.next_run_at)
                if schedule.task_type == MARKETPLACE_TASK_TYPE:
                    if self._enqueue_marketplace(uow, schedule, scheduled_for) is not None:
                        created += 1
                else:
                    runs = (
                        1
                        if schedule.missed_run_policy == "skip"
                        else min(schedule.max_catch_up_runs, 100)
                    )
                    for _ in range(runs):
                        pending.append(
                            JobSubmission(
                                task_type=schedule.task_type,
                                objective=schedule.objective_template,
                                workspace=Path(schedule.workspace_path),
                                requested_agent=schedule.requested_agent,
                                permissions=set(schedule.permissions),
                                context_files=[Path(path) for path in schedule.context_files],
                            )
                        )
                        created += 1
                schedule.last_scheduled_at = now
                schedule.next_run_at = next_run(schedule, now)
                schedule.updated_at = now
        for submission in pending:
            import asyncio

            asyncio.run(self.orchestration_service.submit(submission))
        return created

    def _enqueue_marketplace(self, uow, schedule: Schedule, scheduled_for: datetime):
        metadata = dict(schedule.schedule_metadata or {})
        account_id = metadata.get("marketplace_account_id", "global")
        operation = metadata.get("operation_type", "unknown")
        window = scheduled_for.astimezone(UTC).replace(microsecond=0).isoformat()
        raw_key = f"{schedule.id}:{operation}:{account_id}:{window}"
        idempotency_key = hashlib.sha256(raw_key.encode()).hexdigest()
        job_id = uuid5(NAMESPACE_URL, f"goliath:{idempotency_key}")
        existing = uow.jobs.get(job_id)
        if existing is not None:
            schedule.last_job_id = existing.id
            return None
        trace_id = str(uuid5(NAMESPACE_URL, f"goliath-trace:{idempotency_key}"))
        job_metadata = {
            **metadata,
            "scheduled_timestamp": window,
            "schedule_id": str(schedule.id),
            "idempotency_key": idempotency_key,
            "operation_idempotency_key": idempotency_key,
            "retry_configuration": {
                "max_attempts": self.config.retry_max_attempts,
                "fixed_delay_seconds": self.config.retry_fixed_delay_seconds,
                "exponential": self.config.retry_exponential,
                "max_delay_seconds": self.config.retry_max_delay_seconds,
            },
            "correlation_id": trace_id,
            "trace_id": trace_id,
        }
        record = uow.jobs.create(
            job_id=job_id,
            requested_agent=MARKETPLACE_WORKER_NAME,
            task_type=MARKETPLACE_TASK_TYPE,
            objective=schedule.objective_template,
            workspace_path=schedule.workspace_path,
            permissions=[],
            context_files=[],
            forbidden_actions=sorted(REQUIRED_FORBIDDEN_ACTIONS),
            risk_tier="approval_required"
            if metadata.get("write_classification") == "write_capable"
            else "read_only",
            timeout_seconds=self.config.maximum_timeout_seconds,
            max_output_chars=self.config.default_output_limit_chars,
            job_metadata=job_metadata,
        )
        record = uow.jobs.transition(
            record.id,
            AgentJobStatus.QUEUED,
            expected_version=record.version,
            agent_name=MARKETPLACE_WORKER_NAME,
        )
        schedule.last_job_id = record.id
        uow.audits.append(
            event_type="marketplace.job.scheduled",
            actor_type="system",
            actor_id="marketplace-scheduler",
            resource_type="agent_job",
            resource_id=record.id,
            details={
                "schedule_id": str(schedule.id),
                "operation": operation,
                "account_id": account_id,
                "idempotency_key": idempotency_key,
            },
        )
        return record
