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
media_app = typer.Typer(no_args_is_help=True, help="Ingest and process media.")
comparables_app = typer.Typer(no_args_is_help=True, help="Import and review comparables.")
review_app = typer.Typer(no_args_is_help=True, help="Human review tasks.")
dashboard_app = typer.Typer(no_args_is_help=True, help="Review dashboard.")
automation_app = typer.Typer(no_args_is_help=True, help="Automation and emergency stop.")
marketplace_app = typer.Typer(no_args_is_help=True, help="Manage marketplace accounts.")
order_app = typer.Typer(no_args_is_help=True, help="Marketplace orders.")
offer_app = typer.Typer(no_args_is_help=True, help="Marketplace offers.")
message_app = typer.Typer(no_args_is_help=True, help="Buyer messages.")
shipping_app = typer.Typer(no_args_is_help=True, help="Shipping tasks and labels.")
reconcile_app = typer.Typer(no_args_is_help=True, help="Financial reconciliation.")
breaker_app = typer.Typer(no_args_is_help=True, help="Circuit breakers.")
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
app.add_typer(media_app, name="media")
app.add_typer(comparables_app, name="comparables")
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(automation_app, name="automation")
app.add_typer(marketplace_app, name="marketplace")
app.add_typer(order_app, name="order")
app.add_typer(offer_app, name="offer")
app.add_typer(message_app, name="message")
app.add_typer(shipping_app, name="shipping")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(breaker_app, name="breaker")


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
        factory = build_session_factory(engine)
        from goliath.marketplace.service import MarketplaceService
        from goliath.marketplace.worker import MarketplaceAutomationWorker

        marketplace_service = MarketplaceService(session_factory=factory, config=runtime.config)
        daemon = WorkerDaemon(
            session_factory=factory,
            registry=runtime.registry,
            config=runtime.config,
            marketplace_worker=MarketplaceAutomationWorker(marketplace_service),
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
            {
                "id": str(record.id),
                "status": record.status.value,
                "proposal": str(proposal_id) if proposal_id else None,
            },
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
            {
                "id": str(draft.id),
                "status": draft.status.value,
                "warnings": draft.validation_warnings,
            },
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
            {
                "id": str(variant.id),
                "marketplace": variant.marketplace.value,
                "title": variant.marketplace_title,
            },
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


# --------------------------------------------------------------------------- #
# Milestone five: media, comparables, review, dashboard CLI
# --------------------------------------------------------------------------- #


def build_media_service():
    from goliath.domain.media_service import MediaIngestionService

    config = load_config()
    engine = create_production_engine()
    return MediaIngestionService(session_factory=build_session_factory(engine), config=config)


def build_image_processing_service():
    from goliath.domain.media_service import ImageProcessingService

    config = load_config()
    engine = create_production_engine()
    return ImageProcessingService(session_factory=build_session_factory(engine), config=config)


def build_review_service():
    from goliath.domain.review_service import ReviewService

    config = load_config()
    engine = create_production_engine()
    return ReviewService(session_factory=build_session_factory(engine), config=config)


def build_comparable_import_service():
    from goliath.domain.comparables import ComparableImportService

    config = load_config()
    engine = create_production_engine()
    return ComparableImportService(session_factory=build_session_factory(engine), config=config)


@media_app.command("ingest")
def media_ingest(
    item_id: UUID,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    role: Annotated[str, typer.Option()] = "original",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import MediaRole

    try:
        service = build_media_service()
        media = service.ingest_bytes(
            item_id,
            file.read_bytes(),
            role=MediaRole(role),
            original_filename=file.name,
            actor="human:cli",
        )
        _emit_json(
            {"id": str(media.id), "status": media.status.value},
            json_output=json_output,
            human=f"{media.id} {media.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@media_app.command("list")
def media_list(item_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        svc = build_domain_service()
        rows = [
            {"id": str(m.id), "status": m.status.value, "role": m.role.value}
            for m in svc.list_media(item_id)
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['status']}  {r['role']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@media_app.command("process")
def media_process(
    media_id: Annotated[UUID | None, typer.Argument()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        service = build_image_processing_service()
        if media_id is not None:
            service.enqueue(media_id)
        processed = service.run_next()
        _emit_json(
            {"processed": str(processed) if processed else None},
            json_output=json_output,
            human=f"processed {processed}" if processed else "no queued media jobs",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@media_app.command("quarantine-list")
def media_quarantine_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        service = build_media_service()
        rows = [
            {"id": str(m.id), "findings": m.validation_result.get("findings", [])}
            for m in service.list_quarantined()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['findings']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@media_app.command("archive")
def media_archive(
    media_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        media = build_media_service().archive(media_id, actor="human:cli")
        _emit_json(
            {"id": str(media.id), "status": media.status.value},
            json_output=json_output,
            human=f"{media.id} {media.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@comparables_app.command("import")
def comparables_import(
    item_id: UUID,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source_format: Annotated[str, typer.Option()] = "csv",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.domain.comparables import parse_csv, parse_json

    try:
        service = build_comparable_import_service()
        content = file.read_text()
        rows = parse_csv(content) if source_format == "csv" else parse_json(content)
        summary = service.import_rows(
            item_id, rows, source_format=source_format, actor="human:cli", dry_run=dry_run
        )
        _emit_json(summary, json_output=json_output, human=json.dumps(summary, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@comparables_app.command("review-list")
def comparables_review_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        service = build_review_service()
        rows = [
            {"id": str(c.id), "marketplace": c.marketplace.value, "status": c.review_status.value}
            for c in service.list_comparables_pending()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['marketplace']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@comparables_app.command("accept")
def comparables_accept(
    comparable_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    _comparable_review_cli(comparable_id, "accepted", json_output)


@comparables_app.command("reject")
def comparables_reject(
    comparable_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    _comparable_review_cli(comparable_id, "rejected", json_output)


def _comparable_review_cli(comparable_id: UUID, decision: str, json_output: bool) -> None:
    from goliath.db.models import ComparableReviewStatus

    try:
        comparable = build_review_service().review_comparable(
            comparable_id,
            reviewer="human:cli",
            decision=ComparableReviewStatus(decision),
        )
        _emit_json(
            {"id": str(comparable.id), "review_status": comparable.review_status.value},
            json_output=json_output,
            human=f"{comparable.id} {comparable.review_status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@review_app.command("list")
def review_list(
    status: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from goliath.db.models import ReviewTaskStatus

    try:
        service = build_review_service()
        tasks = service.list_tasks(status=ReviewTaskStatus(status) if status else None)
        rows = [
            {
                "id": str(t.id),
                "type": t.task_type.value,
                "status": t.status.value,
                "version": t.version,
            }
            for t in tasks
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['type']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@review_app.command("claim")
def review_claim(
    task_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        service = build_review_service()
        task = service.get_task(task_id)
        claimed = service.claim_task(task_id, reviewer="human:cli", expected_version=task.version)
        _emit_json(
            {"id": str(claimed.id), "status": claimed.status.value},
            json_output=json_output,
            human=f"{claimed.id} {claimed.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@review_app.command("complete")
def review_complete(
    task_id: UUID,
    outcome: Annotated[str, typer.Option()] = "resolved",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        task = build_review_service().complete_task(task_id, reviewer="human:cli", outcome=outcome)
        _emit_json(
            {"id": str(task.id), "status": task.status.value},
            json_output=json_output,
            human=f"{task.id} {task.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@review_app.command("dismiss")
def review_dismiss(
    task_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        task = build_review_service().dismiss_task(task_id, reviewer="human:cli")
        _emit_json(
            {"id": str(task.id), "status": task.status.value},
            json_output=json_output,
            human=f"{task.id} {task.status.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@dashboard_app.command("summary")
def dashboard_summary(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        summary = build_review_service().summary()
        _emit_json(
            summary, json_output=json_output, human=json.dumps(summary, indent=2, default=str)
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


# --------------------------------------------------------------------------- #
# Milestone six: autonomous marketplace CLI
# --------------------------------------------------------------------------- #


def build_marketplace_service():
    from goliath.marketplace.service import MarketplaceService

    config = load_config()
    engine = create_production_engine()
    return MarketplaceService(session_factory=build_session_factory(engine), config=config)


@automation_app.command("status")
def automation_status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        status = build_marketplace_service().automation_status()
        _emit_json(status, json_output=json_output, human=json.dumps(status, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@automation_app.command("stop")
def automation_stop(
    reason: Annotated[str, typer.Option()] = "operator requested",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = build_marketplace_service().emergency_stop(reason=reason)
        _emit_json(result, json_output=json_output, human=f"stopped: {reason}")
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@automation_app.command("start")
def automation_start(
    reason: Annotated[str, typer.Option()] = "resolved",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = build_marketplace_service().emergency_start(reason=reason)
        _emit_json(result, json_output=json_output, human=f"started: {reason}")
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("account-list")
def marketplace_account_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        rows = [
            {
                "id": str(a.id),
                "marketplace": a.marketplace.value,
                "mode": a.automation_mode.value,
                "status": a.status.value,
            }
            for a in build_marketplace_service().list_accounts()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(
                f"{r['id']}  {r['marketplace']}  {r['mode']}  {r['status']}" for r in rows
            ),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("mode")
def marketplace_mode(
    account_id: UUID,
    mode: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        account = build_marketplace_service().set_mode(account_id, mode=mode.replace("-", "_"))
        _emit_json(
            {"id": str(account.id), "mode": account.automation_mode.value},
            json_output=json_output,
            human=f"{account.id} {account.automation_mode.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("authenticate")
def marketplace_authenticate(
    account_id: UUID,
    session_state: Annotated[Path, typer.Option("--session-state")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Import Playwright storage state through the broker; never print its contents."""
    try:
        raw = session_state.expanduser().resolve().read_bytes()
        build_marketplace_service().authenticate_account(account_id, raw, actor="human:cli")
        _emit_json(
            {"authenticated": True, "account_id": str(account_id)},
            json_output=json_output,
            human=f"authenticated {account_id}",
        )
    except (OSError, RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("sync")
def marketplace_sync(
    account_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        result = asyncio.run(
            build_marketplace_service().sync_orders(account_id, principal_scopes={"admin"})
        )
        _emit_json(result, json_output=json_output, human=json.dumps(result, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


def _marketplace_scheduler():
    runtime = build_runtime()
    engine = create_production_engine()
    from goliath.orchestration.scheduler import SchedulerService

    return SchedulerService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(build_session_factory(engine)),
        orchestration_service=runtime.service,
        config=runtime.config,
    )


@marketplace_app.command("schedules-install")
def marketplace_schedules_install(
    account_id: Annotated[list[UUID] | None, typer.Option("--account-id")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = _marketplace_scheduler().install_marketplace_schedules(
            account_ids=set(account_id) if account_id else None
        )
        _emit_json(
            result,
            json_output=json_output,
            human=f"created={result['created']} existing={result['existing']}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("schedules-list")
def marketplace_schedules_list(
    account_id: Annotated[UUID | None, typer.Option("--account-id")] = None,
    operation: Annotated[str | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import ScheduleRepository

            values = [
                schedule
                for schedule in ScheduleRepository(session).list()
                if schedule.task_type == "marketplace_operation"
                and (
                    account_id is None
                    or (schedule.schedule_metadata or {}).get("marketplace_account_id")
                    == str(account_id)
                )
                and (
                    operation is None
                    or (schedule.schedule_metadata or {}).get("operation_type") == operation
                )
            ]
        payload = [
            {
                "id": str(value.id),
                "name": value.name,
                "enabled": value.enabled,
                "operation": value.schedule_metadata.get("operation_type"),
                "account_id": value.schedule_metadata.get("marketplace_account_id"),
                "last_run_at": value.last_scheduled_at,
                "next_run_at": value.next_run_at,
                "last_job_id": str(value.last_job_id) if value.last_job_id else None,
            }
            for value in values
        ]
        _emit_json(
            payload,
            json_output=json_output,
            human="\n".join(
                f"{row['id']}  {row['operation']}  {'enabled' if row['enabled'] else 'disabled'}"
                for row in payload
            ),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


def _marketplace_schedule_enabled(schedule_id: UUID, enabled: bool, json_output: bool) -> None:
    try:
        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            from goliath.db.operations import ScheduleRepository

            schedule = ScheduleRepository(session).get(schedule_id)
            if schedule is None or schedule.task_type != "marketplace_operation":
                raise RecordNotFoundError(f"marketplace schedule not found: {schedule_id}")
            schedule = ScheduleRepository(session).set_enabled(schedule_id, enabled)
            session.commit()
        _emit_json(
            {"id": str(schedule.id), "enabled": schedule.enabled},
            json_output=json_output,
            human=f"{schedule.id} {'enabled' if enabled else 'disabled'}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@marketplace_app.command("schedules-enable")
def marketplace_schedules_enable(
    schedule_id: UUID,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _marketplace_schedule_enabled(schedule_id, True, json_output)


@marketplace_app.command("schedules-disable")
def marketplace_schedules_disable(
    schedule_id: UUID,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _marketplace_schedule_enabled(schedule_id, False, json_output)


@marketplace_app.command("schedules-run-now")
def marketplace_schedules_run_now(
    schedule_id: UUID,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        job = _marketplace_scheduler().run_now(schedule_id)
        payload = {
            "job_id": str(job.id) if job is not None else None,
            "existing": job is None,
        }
        _emit_json(
            payload,
            json_output=json_output,
            human=(str(job.id) if job is not None else "job already exists for this window"),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@listing_app.command("publish")
def listing_publish(
    draft_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        result = asyncio.run(
            build_marketplace_service().publish_draft(draft_id, principal_scopes={"admin"})
        )
        _emit_json(result, json_output=json_output, human=json.dumps(result, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@listing_app.command("refresh")
def listing_refresh(
    listing_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    _listing_write(listing_id, "refresh", json_output)


@listing_app.command("promote")
def listing_promote(
    listing_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    _listing_write(listing_id, "promote", json_output)


@listing_app.command("end")
def listing_end(
    listing_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    _listing_write(listing_id, "end", json_output)


@listing_app.command("sync")
def listing_sync(
    listing_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        receipt = asyncio.run(
            build_marketplace_service().read_listing(
                listing_id, principal_scopes={"admin"}, actor="human:cli"
            )
        )
        result = {"ok": receipt.ok, "data": receipt.data}
        _emit_json(result, json_output=json_output, human=json.dumps(result, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


def _listing_write(listing_id: UUID, action: str, json_output: bool) -> None:
    try:
        service = build_marketplace_service()
        method = {
            "refresh": service.refresh_listing,
            "promote": service.promote_listing,
            "end": service.end_listing,
        }[action]
        receipt = asyncio.run(method(listing_id, principal_scopes={"admin"}))
        _emit_json(
            {"ok": receipt.ok, "verified": receipt.verified},
            json_output=json_output,
            human=f"{action} ok={receipt.ok}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@order_app.command("list")
def order_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        rows = [
            {"id": str(o.id), "status": o.status.value, "sale_price": str(o.sale_price)}
            for o in build_marketplace_service().list_orders()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@order_app.command("sync")
def order_sync(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        service = build_marketplace_service()
        results = [
            asyncio.run(service.sync_orders(a.id, principal_scopes={"admin"}))
            for a in service.list_accounts()
        ]
        _emit_json(results, json_output=json_output, human=json.dumps(results, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@offer_app.command("list")
def offer_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        rows = [
            {"id": str(o.id), "status": o.status.value, "amount": str(o.offer_amount)}
            for o in build_marketplace_service().list_offers()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@message_app.command("list")
def message_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        from goliath.db.marketplace_repositories import MessageRepository

        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            rows = [
                {"id": str(t.id), "escalated": t.escalated, "category": t.last_category}
                for t in MessageRepository(session).list_threads()
            ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['category']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@shipping_app.command("list")
def shipping_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        from goliath.db.marketplace_repositories import ShippingTaskRepository

        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            rows = [
                {"id": str(t.id), "status": t.status.value, "profile": t.package_profile}
                for t in ShippingTaskRepository(session).list()
            ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['status']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@shipping_app.command("purchase-label")
def shipping_purchase_label(
    task_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        result = asyncio.run(
            build_marketplace_service().purchase_label(task_id, principal_scopes={"admin"})
        )
        _emit_json(result, json_output=json_output, human=json.dumps(result, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@reconcile_app.command("run")
def reconcile_run(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        results = build_marketplace_service().run_reconciliation()
        _emit_json(results, json_output=json_output, human=json.dumps(results, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@reconcile_app.command("issues")
def reconcile_issues(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        from goliath.db.marketplace_repositories import ReconciliationRepository

        engine = create_production_engine()
        with build_session_factory(engine)() as session:
            rows = [
                {"id": str(r.id), "order_id": str(r.order_id), "discrepancies": r.discrepancies}
                for r in ReconciliationRepository(session).list_discrepancies()
            ]
        _emit_json(rows, json_output=json_output, human=json.dumps(rows, default=str))
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@breaker_app.command("list")
def breaker_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        rows = [
            {"id": str(b.id), "scope": b.scope, "key": b.scope_key, "state": b.state.value}
            for b in build_marketplace_service().list_breakers()
        ]
        _emit_json(
            rows,
            json_output=json_output,
            human="\n".join(f"{r['id']}  {r['scope']}  {r['state']}" for r in rows),
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)


@breaker_app.command("reset")
def breaker_reset(
    breaker_id: UUID, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    try:
        breaker = build_marketplace_service().reset_breaker(breaker_id)
        _emit_json(
            {"id": str(breaker.id), "state": breaker.state.value},
            json_output=json_output,
            human=f"{breaker.id} {breaker.state.value}",
        )
    except (RuntimeError, ValueError, LookupError) as error:
        _fail(error)
