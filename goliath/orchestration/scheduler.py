from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from goliath.core.schemas import JobSubmission
from goliath.db.models import Schedule, utc_now
from goliath.db.operations import ScheduleRepository

SAFE_SCHEDULE_TASKS = {"code_change", "test", "review", "research", "analysis", "operations"}


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    zone = ZoneInfo(schedule.timezone)
    point = after.astimezone(zone)
    if schedule.timing_type == "once":
        value = (
            datetime.fromisoformat(schedule.timing_value).replace(tzinfo=zone)
            if datetime.fromisoformat(schedule.timing_value).tzinfo is None
            else datetime.fromisoformat(schedule.timing_value)
        )
        return value.astimezone(UTC) if value > point else None
    if schedule.timing_type == "interval":
        candidate = point + timedelta(seconds=float(schedule.timing_value))
        return candidate.astimezone(UTC)
    # Supported cron subset: "@hourly", "@daily", or "*/N * * * *".
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

    def tick(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        created = 0
        with self.uow_factory() as uow:
            schedules = ScheduleRepository(uow.session).list(enabled=True)
            for schedule in schedules:
                if schedule.next_run_at is None or schedule.next_run_at > now:
                    continue
                runs = (
                    1
                    if schedule.missed_run_policy == "skip"
                    else min(schedule.max_catch_up_runs, 100)
                )
                for _ in range(runs):
                    submission = JobSubmission(
                        task_type=schedule.task_type,
                        objective=schedule.objective_template,
                        workspace=Path(schedule.workspace_path),
                        requested_agent=schedule.requested_agent,
                        permissions=set(schedule.permissions),
                        context_files=[Path(path) for path in schedule.context_files],
                    )
                    # Queue through the normal orchestration path after this transaction.
                    uow.pending_submissions.append(submission)
                    created += 1
                schedule.last_scheduled_at = now
                schedule.next_run_at = next_run(schedule, now)
            pending = getattr(uow, "pending_submissions", [])
        for submission in pending:
            import asyncio

            asyncio.run(self.orchestration_service.submit(submission))
        return created
