from __future__ import annotations

import asyncio
import json
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import ValidationError

from goliath.agents.registry import AgentRegistry, build_registry
from goliath.config import OrchestrationConfig, load_config
from goliath.core.schemas import (
    REQUIRED_FORBIDDEN_ACTIONS,
    AgentPermission,
    CancellationRequest,
    CapabilityName,
    JobStatus,
    JobSubmission,
    RiskTier,
    TaskType,
)
from goliath.db.database import build_session_factory, create_production_engine
from goliath.db.repositories import InvalidStateTransitionError, RecordNotFoundError
from goliath.orchestration.service import JobOrchestrationService, OrchestrationError
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from goliath.orchestration.worker import WorkerDaemon

app = typer.Typer(no_args_is_help=True)
agent_app = typer.Typer(no_args_is_help=True, help="Inspect configured CLI agents.")
job_app = typer.Typer(no_args_is_help=True, help="Submit and operate persistent agent jobs.")
worker_app = typer.Typer(no_args_is_help=True, help="Run durable workers.")
auth_app = typer.Typer(no_args_is_help=True, help="Manage local API keys.")
schedule_app = typer.Typer(no_args_is_help=True, help="Manage safe internal schedules.")
inventory_app = typer.Typer(no_args_is_help=True, help="Manage resale inventory.")
research_app = typer.Typer(no_args_is_help=True, help="Manage product research.")
pricing_app = typer.Typer(no_args_is_help=True, help="Deterministic pricing.")
listing_app = typer.Typer(no_args_is_help=True, help="Manage listing drafts and variants.")
approval_app = typer.Typer(no_args_is_help=True, help="Review domain proposals.")
app.add_typer(agent_app, name="agent")
app.add_typer(job_app, name="job")
app.add_typer(worker_app, name="worker")
app.add_typer(auth_app, name="auth")
app.add_typer(schedule_app, name="schedule")
app.add_typer(inventory_app, name="inventory")
app.add_typer(research_app, name="research")
app.add_typer(pricing_app, name="pricing")
app.add_typer(listing_app, name="listing")
app.add_typer(approval_app, name="approval")


@dataclass(slots=True)
class Runtime:
    config: OrchestrationConfig
    registry: AgentRegistry
    service: JobOrchestrationService


def build_runtime() -> Runtime:
    config, registry = build_agent_registry()
    engine = create_production_engine()
    session_factory = build_session_factory(engine)
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(session_factory),
        registry=registry,
        config=config,
    )
    return Runtime(config=config, registry=registry, service=service)


def build_agent_registry() -> tuple[OrchestrationConfig, AgentRegistry]:
    config = load_config()
    return config, build_registry(config)


def _emit(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "model_dump_json"):
        typer.echo(value.model_dump_json(indent=2) if json_output else _human_job(value))
    elif json_output:
        typer.echo(json.dumps(value, indent=2, default=str))
    elif isinstance(value, list):
        for item in value:
            typer.echo(item)
    else:
        typer.echo(value)


def _human_job(job: Any) -> str:
    return f"{job.id}  {job.status.value}  {job.agent_name or '-'}  {job.objective}"


def _fail(error: Exception) -> None:
    typer.echo(f"error: {error}", err=True)
    raise typer.Exit(code=1)


@agent_app.command("list")
def agent_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        _, registry = build_agent_registry()
        agents = [
            {
                "name": name,
                "priority": entry.priority,
                "capabilities": [
                    capability.name.value for capability in entry.adapter.capabilities
                ],
            }
            for name, entry in registry.entries()
        ]
        _emit(agents if json_output else [item["name"] for item in agents], json_output=json_output)
    except (FileNotFoundError, RuntimeError, ValueError, ValidationError) as error:
        _fail(error)


@agent_app.command("doctor")
def agent_doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        _, registry = build_agent_registry()
        results = asyncio.run(registry.health())
        if json_output:
            _emit([result.model_dump(mode="json") for result in results], json_output=True)
        else:
            _emit(
                [
                    f"{result.agent}: {'available' if result.available else 'unavailable'}"
                    for result in results
                ],
                json_output=False,
            )
        if not all(result.available for result in results):
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (FileNotFoundError, RuntimeError, ValueError, ValidationError) as error:
        _fail(error)


@job_app.command("submit")
def job_submit(
    objective: Annotated[str, typer.Option(help="Focused coding or research objective.")],
    workspace: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, dir_okay=True, help="Approved workspace."),
    ],
    agent: Annotated[str | None, typer.Option(help="Explicit agent name.")] = None,
    task_type: Annotated[TaskType, typer.Option()] = TaskType.CODE_CHANGE,
    permission: Annotated[list[AgentPermission] | None, typer.Option("--permission")] = None,
    capability: Annotated[list[CapabilityName] | None, typer.Option("--capability")] = None,
    context_file: Annotated[list[Path] | None, typer.Option("--context-file")] = None,
    timeout: Annotated[int | None, typer.Option(min=1)] = None,
    output_limit: Annotated[int | None, typer.Option(min=1)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        runtime = build_runtime()
        submission = JobSubmission(
            requested_agent=agent,
            task_type=task_type,
            objective=objective,
            workspace=workspace,
            permissions=set(permission or [AgentPermission.READ_FILES]),
            context_files=context_file or [],
            forbidden_actions=set(REQUIRED_FORBIDDEN_ACTIONS),
            required_capabilities=set(capability or []),
            risk_tier=RiskTier.DRAFT,
            timeout_seconds=timeout or runtime.config.default_timeout_seconds,
            max_output_chars=output_limit or runtime.config.default_output_limit_chars,
        )
        response = asyncio.run(runtime.service.submit(submission))
        _emit(response, json_output=json_output)
        if response.status is JobStatus.FAILED:
            raise typer.Exit(code=1)
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        ValidationError,
        OrchestrationError,
    ) as error:
        _fail(error)


@job_app.command("status")
def job_status(
    job_id: UUID,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        _emit(build_runtime().service.get(job_id), json_output=json_output)
    except (FileNotFoundError, RuntimeError, ValueError, RecordNotFoundError) as error:
        _fail(error)


@job_app.command("cancel")
def job_cancel(
    job_id: UUID,
    reason: Annotated[str, typer.Option(help="Cancellation reason.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        response = asyncio.run(
            build_runtime().service.cancel(job_id, CancellationRequest(reason=reason))
        )
        _emit(response, json_output=json_output)
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        RecordNotFoundError,
        InvalidStateTransitionError,
    ) as error:
        _fail(error)


@job_app.command("list")
def job_list(
    status: Annotated[JobStatus | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        jobs = build_runtime().service.list(status=status, limit=limit)
        if json_output:
            _emit([job.model_dump(mode="json") for job in jobs], json_output=True)
        else:
            _emit([_human_job(job) for job in jobs], json_output=False)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


@job_app.command("run-next")
def job_run_next(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        result = asyncio.run(build_runtime().service.run_next())
        if result is None:
            typer.echo("no queued jobs", err=True)
            raise typer.Exit(code=1)
        _emit(result, json_output=json_output)
        if result.status is not JobStatus.SUCCEEDED:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


# Compatibility alias for the original command name.
@app.command("doctor", hidden=True)
def legacy_doctor() -> None:
    agent_doctor()


@worker_app.command("start")
def worker_start() -> None:
    try:
        runtime = build_runtime()
        engine = create_production_engine()
        daemon = WorkerDaemon(
            session_factory=build_session_factory(engine),
            registry=runtime.registry,
            config=runtime.config,
        )

        async def run_worker() -> None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, daemon.request_shutdown)
                except (NotImplementedError, RuntimeError):
                    pass
            await daemon.run()

        asyncio.run(run_worker())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


@worker_app.command("status")
def worker_status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import WorkerRepository

            values = WorkerRepository(session).list()
        payload = [
            {
                "id": value.id,
                "status": value.status.value,
                "last_heartbeat_at": value.last_heartbeat_at.isoformat(),
                "active_job_count": value.active_job_count,
            }
            for value in values
        ]
        _emit(
            payload if json_output else [f"{item['id']}: {item['status']}" for item in payload],
            json_output=json_output,
        )
    except (RuntimeError, ValueError) as error:
        _fail(error)


@worker_app.command("heartbeat")
def worker_heartbeat(worker_id: str) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import WorkerRepository

            WorkerRepository(session).heartbeat(worker_id)
            session.commit()
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@worker_app.command("reap")
def worker_reap() -> None:
    try:
        runtime = build_runtime()
        engine = create_production_engine()
        daemon = WorkerDaemon(
            session_factory=build_session_factory(engine),
            registry=runtime.registry,
            config=runtime.config,
            worker_id="reaper",
        )
        typer.echo(str(daemon.reap()))
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


@auth_app.command("key-create")
def auth_key_create(
    name: str, scope: Annotated[list[str] | None, typer.Option("--scope")] = None
) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import AuthRepository

            _, key, raw = AuthRepository(session).create_key(name, scope or ["admin"])
            session.commit()
        typer.echo(json.dumps({"key_id": str(key.id), "api_key": raw, "warning": "shown once"}))
    except (RuntimeError, ValueError) as error:
        _fail(error)


@auth_app.command("key-list")
def auth_key_list() -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import AuthRepository

            typer.echo(
                json.dumps(
                    [
                        {
                            "id": str(key.id),
                            "prefix": key.key_prefix,
                            "revoked": key.revoked_at is not None,
                        }
                        for key in AuthRepository(session).list_keys()
                    ]
                )
            )
    except (RuntimeError, ValueError) as error:
        _fail(error)


@auth_app.command("key-revoke")
def auth_key_revoke(key_id: UUID) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import AuthRepository

            AuthRepository(session).revoke(key_id)
            session.commit()
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@schedule_app.command("create")
def schedule_create(
    name: str, task_type: TaskType, objective: str, workspace: Path, interval_seconds: int = 3600
) -> None:
    try:
        runtime = build_runtime()
        engine = create_production_engine()
        from goliath.orchestration.scheduler import SchedulerService

        service = SchedulerService(
            uow_factory=lambda: SqlAlchemyJobUnitOfWork(build_session_factory(engine)),
            orchestration_service=runtime.service,
            config=runtime.config,
        )
        schedule = service.create(
            name=name,
            task_type=task_type.value,
            objective_template=objective,
            workspace=workspace,
            timing_type="interval",
            timing_value=str(interval_seconds),
            owner="cli",
        )
        typer.echo(str(schedule.id))
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


@schedule_app.command("list")
def schedule_list() -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import ScheduleRepository

            for schedule in ScheduleRepository(session).list():
                typer.echo(
                    f"{schedule.id}  {schedule.name}  {'enabled' if schedule.enabled else 'disabled'}"
                )
    except (RuntimeError, ValueError) as error:
        _fail(error)


@schedule_app.command("enable")
def schedule_enable(schedule_id: UUID) -> None:
    _schedule_set_enabled(schedule_id, True)


@schedule_app.command("disable")
def schedule_disable(schedule_id: UUID) -> None:
    _schedule_set_enabled(schedule_id, False)


def _schedule_set_enabled(schedule_id: UUID, enabled: bool) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import ScheduleRepository

            ScheduleRepository(session).set_enabled(schedule_id, enabled)
            session.commit()
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@schedule_app.command("tick")
def scheduler_tick() -> None:
    try:
        runtime = build_runtime()
        engine = create_production_engine()
        from goliath.orchestration.scheduler import SchedulerService

        service = SchedulerService(
            uow_factory=lambda: SqlAlchemyJobUnitOfWork(build_session_factory(engine)),
            orchestration_service=runtime.service,
            config=runtime.config,
        )
        typer.echo(str(service.tick()))
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        _fail(error)


# --------------------------------------------------------------------------- #
# Milestone four: resale-domain CLI
# --------------------------------------------------------------------------- #


def build_domain_service():
    from goliath.domain.service import DomainService

    config = load_config()
    engine = create_production_engine()
    return DomainService(session_factory=build_session_factory(engine), config=config)


def _emit_json(value: object, *, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(json.dumps(value, indent=2, default=str))
    else:
        typer.echo(human)


@inventory_app.command("create")
def inventory_create(
    sku: Annotated[str, typer.Option()],
    title: Annotated[str, typer.Option()],
    cost: Annotated[float, typer.Option(help="Acquisition cost.")],
    condition: Annotated[str, typer.Option()] = "good",
    category: Annotated[str | None, typer.Option()] = None,
    brand: Annotated[str | None, typer.Option()] = None,
    size: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from decimal import Decimal

    try:
        service = build_domain_service()
        item = service.create_inventory(
            actor="human:cli",
            sku=sku,
            title=title,
            condition=condition,
            acquisition_cost=Decimal(str(cost)),
            category=category,
            brand=brand,
            size_label=size,
        )
        _emit_json(
            {"id": str(item.id), "sku": item.sku, "status": item.status.value},
            json_output=json_output,
            human=str(item.id),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@inventory_app.command("list")
def inventory_list(
    status: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import InventoryStatus

    try:
        service = build_domain_service()
        items = service.list_inventory(
            status=InventoryStatus(status) if status else None, limit=limit
        )
        rows = [
            {"id": str(i.id), "sku": i.sku, "status": i.status.value, "title": i.title}
            for i in items
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['status']}  {r['sku']}  {r['title']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@inventory_app.command("show")
def inventory_show(
    item_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        item = build_domain_service().get_inventory(item_id)
        payload = {
            "id": str(item.id),
            "sku": item.sku,
            "title": item.title,
            "brand": item.brand,
            "status": item.status.value,
            "version": item.version,
        }
        _emit_json(payload, json_output=json_output, human=json.dumps(payload, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@inventory_app.command("completeness")
def inventory_completeness(
    item_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        result = build_domain_service().evaluate_completeness(item_id)
        _emit_json(
            result.model_dump(),
            json_output=json_output,
            human=f"score={result.score} blocking={result.blocking_errors}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@research_app.command("create")
def research_create(
    item_id: UUID,
    question: Annotated[str, typer.Option()],
    researcher: Annotated[str, typer.Option()] = "human:cli",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        record = build_domain_service().create_research(
            item_id, actor="human:cli", research_question=question, researcher=researcher
        )
        _emit_json(
            {"id": str(record.id), "status": record.status.value},
            json_output=json_output,
            human=str(record.id),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@research_app.command("show")
def research_show(
    research_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        record = build_domain_service().get_research(research_id)
        payload = {
            "id": str(record.id),
            "status": record.status.value,
            "confidence": str(record.confidence) if record.confidence is not None else None,
        }
        _emit_json(payload, json_output=json_output, human=json.dumps(payload, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@research_app.command("complete")
def research_complete(
    research_id: UUID,
    status: Annotated[str, typer.Option()] = "completed",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import ResearchStatus

    try:
        record, proposal_id = build_domain_service().complete_research(
            research_id, actor="human:cli", status=ResearchStatus(status)
        )
        _emit_json(
            {"id": str(record.id), "status": record.status.value, "proposal": str(proposal_id) if proposal_id else None},
            json_output=json_output,
            human=f"{record.id} {record.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@pricing_app.command("calculate")
def pricing_calculate(
    item_id: UUID,
    cost_basis: Annotated[float, typer.Option()],
    fee_version: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from decimal import Decimal

    try:
        result = build_domain_service().calculate_pricing_from_comparables(
            item_id, actor="human:cli", cost_basis=Decimal(str(cost_basis)), fee_version=fee_version
        )
        _emit_json(
            result.model_dump(mode="json"),
            json_output=json_output,
            human=(
                f"recommended={result.recommended_price} "
                f"fast_sale={result.fast_sale_price} minimum={result.minimum_price}"
            ),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@listing_app.command("draft-create")
def listing_draft_create(
    item_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        draft = build_domain_service().create_master_draft(item_id, actor="human:cli")
        _emit_json(
            {"id": str(draft.id), "status": draft.status.value, "warnings": draft.validation_warnings},
            json_output=json_output,
            human=str(draft.id),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@listing_app.command("validate")
def listing_validate(
    draft_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        result = build_domain_service().validate_draft(draft_id, actor="human:cli")
        _emit_json(result, json_output=json_output, human=json.dumps(result, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@listing_app.command("variant-create")
def listing_variant_create(
    draft_id: UUID,
    marketplace: Annotated[str, typer.Option()] = "ebay",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import Marketplace

    try:
        variant = build_domain_service().create_variant(
            draft_id, actor="human:cli", marketplace=Marketplace(marketplace)
        )
        _emit_json(
            {"id": str(variant.id), "marketplace": variant.marketplace.value, "title": variant.marketplace_title},
            json_output=json_output,
            human=variant.marketplace_title,
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@approval_app.command("list")
def approval_list(
    status: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import ProposalStatus

    try:
        proposals = build_domain_service().list_proposals(
            status=ProposalStatus(status) if status else None
        )
        rows = [
            {"id": str(p.id), "type": p.proposal_type.value, "status": p.status.value}
            for p in proposals
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['type']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@approval_app.command("show")
def approval_show(
    approval_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        p = build_domain_service().get_proposal(approval_id)
        payload = {
            "id": str(p.id),
            "type": p.proposal_type.value,
            "status": p.status.value,
            "justification": p.justification,
        }
        _emit_json(payload, json_output=json_output, human=json.dumps(payload, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@approval_app.command("approve")
def approval_approve(
    approval_id: UUID,
    reviewer: Annotated[str, typer.Option()] = "human:operator",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        p = build_domain_service().approve_proposal(approval_id, reviewer=reviewer)
        _emit_json(
            {"id": str(p.id), "status": p.status.value, "execution_error": p.execution_error},
            json_output=json_output,
            human=f"{p.id} {p.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@approval_app.command("reject")
def approval_reject(
    approval_id: UUID,
    reviewer: Annotated[str, typer.Option()] = "human:operator",
    reason: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        p = build_domain_service().reject_proposal(approval_id, reviewer=reviewer, reason=reason)
        _emit_json(
            {"id": str(p.id), "status": p.status.value},
            json_output=json_output,
            human=f"{p.id} {p.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)
