import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from goliath.config import OrchestrationConfig
from goliath.core.schemas import CancellationRequest, JobStatus, JobSubmission
from goliath.db.models import (
    AgentJobRecord,
    AgentJobStatus,
    ApiPrincipal,
    WorkerRecord,
    WorkerStatus,
)
from goliath.db.operations import AuthRepository, IdempotencyRepository, WorkerRepository
from goliath.db.repositories import AuditEventRepository, RecordNotFoundError


class JobCreateRequest(JobSubmission):
    pass


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class ScheduleCreateRequest(BaseModel):
    name: str
    task_type: str
    objective_template: str
    workspace: str
    timing_type: str = "interval"
    timing_value: str = "3600"
    timezone: str = "UTC"
    requested_agent: str | None = None
    permissions: list[str] = ["read_files"]
    context_files: list[str] = []


class InventoryCreateRequest(BaseModel):
    sku: str
    title: str
    condition: str = "good"
    acquisition_cost: float = Field(ge=0)
    description: str | None = None
    category: str | None = None
    subcategory: str | None = None
    brand: str | None = None
    size_label: str | None = None
    colors: list[str] = []
    materials: list[str] = []
    status: str = "draft"


class InventoryDraftUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    changes: dict


class MeasurementCreateRequest(BaseModel):
    measurement_type: str
    value: float = Field(gt=0)
    unit: str
    method: str | None = None
    confidence: float | None = None
    source: str | None = None
    notes: str | None = None


class ResearchCreateRequest(BaseModel):
    research_question: str
    researcher: str
    search_terms: list[str] = []


class PricingCalculateRequest(BaseModel):
    item_id: str
    cost_basis: float = Field(ge=0)
    currency: str = "USD"
    condition: str = "good"
    fee_version: str | None = None
    shipping_cost: float = Field(default=0, ge=0)
    is_stale: bool = False
    comparables: list[dict] = []


class ListingDraftCreateRequest(BaseModel):
    item_id: str


class ApprovalDecisionRequest(BaseModel):
    reviewer: str = "human:operator"
    notes: str | None = None
    reason: str | None = None


class ComparableImportRequest(BaseModel):
    item_id: str
    source_format: str = "json"
    content: str
    dry_run: bool = False
    all_or_nothing: bool = False


class ComparableReviewRequest(BaseModel):
    reviewer: str = "human:operator"
    similarity_score: float | None = None
    reliability_score: float | None = None
    notes: str | None = None


class ReviewTaskDecisionRequest(BaseModel):
    reviewer: str = "human:operator"
    outcome: str = "resolved"
    notes: str | None = None
    expected_version: int | None = None


class BulkRequest(BaseModel):
    ids: list[str]
    all_or_nothing: bool = False
    reviewer: str = "human:operator"
    outcome: str = "resolved"
    decision: str | None = None


class AutomationCommandRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
    scope_key: str = "*"


class MarketplaceAccountCreateRequest(BaseModel):
    marketplace: str
    label: str
    currency: str = "USD"
    capabilities: list[str] = []


class ModeChangeRequest(BaseModel):
    mode: str
    expected_version: int | None = None


class AuthenticateRequest(BaseModel):
    session_state_b64: str


class MarketplaceConnectRequest(BaseModel):
    account_id: UUID
    sandbox: bool = True


class MarketplaceReauthenticateRequest(BaseModel):
    sandbox: bool = True


class OAuthCallbackRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=20, max_length=512)
    sandbox: bool = True


class PublishRequest(BaseModel):
    item_id: str
    account_ids: list[str] | None = None


class MessageRespondRequest(BaseModel):
    body: str
    facts: dict = {}


class ListingUpdateRequest(BaseModel):
    fields: dict = {}


class TrackingUpdateRequest(BaseModel):
    tracking_number: str
    carrier: str


def _listing_scope(path: str, method: str) -> str:
    if method == "GET":
        return "marketplace:read"
    if path.endswith("/refresh"):
        return "marketplace:listing:refresh"
    if path.endswith("/promote"):
        return "marketplace:listing:promote"
    if path.endswith("/share"):
        return "marketplace:listing:share"
    if path.endswith("/update"):
        return "marketplace:listing:update"
    if path.endswith("/end"):
        return "marketplace:listing:end"
    return "marketplace:listing:create"


def create_app(
    *, service, registry, session_factory, config: OrchestrationConfig, marketplace_service=None
) -> FastAPI:
    app = FastAPI(title="Resale Goliath Orchestration API", version="0.1.0")
    from goliath.domain.media_service import ImageProcessingService, MediaIngestionService
    from goliath.domain.review_service import ReviewService
    from goliath.domain.service import DomainService

    domain_service = DomainService(session_factory=session_factory, config=config)
    media_service = MediaIngestionService(session_factory=session_factory, config=config)
    image_processing_service = ImageProcessingService(
        session_factory=session_factory, config=config
    )
    review_service = ReviewService(session_factory=session_factory, config=config)
    if marketplace_service is None:
        from goliath.marketplace.service import MarketplaceService

        marketplace_service = MarketplaceService(session_factory=session_factory, config=config)

    def require_scope(scope: str):
        def dependency(
            authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        ) -> ApiPrincipal | None:
            if not config.api_authentication_required:
                return None
            if not authorization or not authorization.lower().startswith("bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required"
                )
            with session_factory() as session:
                authenticated = AuthRepository(session).authenticate(authorization[7:])
                if authenticated is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key"
                    )
                result, key = authenticated
                if scope not in key.scopes and "admin" not in key.scopes:
                    raise HTTPException(status_code=403, detail=f"scope required: {scope}")
                session.commit()
                return result

        return dependency

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            with session_factory() as session:
                session.execute(select(WorkerRecord.id).limit(1)).all()
            return {"status": "ready"}
        except Exception as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error

    @app.get("/agents", dependencies=[Depends(require_scope("agents:read"))])
    async def agents():
        return [
            {
                "name": name,
                "priority": entry.priority,
                "capabilities": [cap.name.value for cap in entry.adapter.capabilities],
            }
            for name, entry in registry.entries()
        ]

    @app.get("/jobs", dependencies=[Depends(require_scope("jobs:read"))])
    async def jobs(
        status_filter: JobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        values = service.list(status=status_filter, limit=limit + offset)
        return values[offset:]

    @app.post("/jobs", status_code=201)
    async def create_job(
        request: Request,
        payload: JobCreateRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("jobs:write"))],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if idempotency_key and current is None and config.api_authentication_required:
            authorization = request.headers.get("Authorization", "")
            with session_factory() as auth_session:
                authenticated = (
                    AuthRepository(auth_session).authenticate(authorization[7:])
                    if authorization.lower().startswith("bearer ")
                    else None
                )
                if authenticated is not None:
                    current, _ = authenticated
                auth_session.commit()
        if idempotency_key and current is not None:
            request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
            with session_factory() as session:
                idem = IdempotencyRepository(session)
                existing = idem.get(current.id, idempotency_key)
                if existing:
                    if existing.request_hash != request_hash:
                        raise HTTPException(
                            status_code=409, detail="idempotency key payload conflict"
                        )
                    return service.get(existing.resource_id)
                result = await service.submit(payload)
                idem.create(
                    current.id,
                    idempotency_key,
                    request_hash,
                    "agent_job",
                    result.id,
                    datetime.now(UTC) + timedelta(seconds=config.idempotency_expiration_seconds),
                )
                session.commit()
                return result
        return await service.submit(payload)

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_scope("jobs:read"))])
    async def job(job_id: UUID):
        try:
            return service.get(job_id)
        except RecordNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_scope("jobs:cancel"))])
    async def cancel(
        job_id: UUID,
        payload: CancelRequest,
    ):
        try:
            return await service.cancel(job_id, CancellationRequest(reason=payload.reason))
        except RecordNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/jobs/{job_id}/audit", dependencies=[Depends(require_scope("audit:read"))])
    async def audit(job_id: UUID):
        with session_factory() as session:
            return AuditEventRepository(session).list_for_resource("agent_job", job_id)

    @app.get("/workers", dependencies=[Depends(require_scope("workers:read"))])
    async def workers():
        with session_factory() as session:
            return WorkerRepository(session).list()

    @app.get("/workers/{worker_id}", dependencies=[Depends(require_scope("workers:read"))])
    async def worker(
        worker_id: str,
    ):
        with session_factory() as session:
            value = WorkerRepository(session).get(worker_id)
            if value is None:
                raise HTTPException(status_code=404, detail="worker not found")
            return value

    @app.get("/schedules", dependencies=[Depends(require_scope("schedules:read"))])
    async def schedules():
        from goliath.db.operations import ScheduleRepository

        with session_factory() as session:
            return ScheduleRepository(session).list()

    @app.get("/schedules/{schedule_id}", dependencies=[Depends(require_scope("schedules:read"))])
    async def schedule(
        schedule_id: UUID,
    ):
        from goliath.db.operations import ScheduleRepository

        with session_factory() as session:
            value = ScheduleRepository(session).get(schedule_id)
            if value is None:
                raise HTTPException(status_code=404, detail="schedule not found")
            return value

    @app.post(
        "/schedules", status_code=201, dependencies=[Depends(require_scope("schedules:write"))]
    )
    async def create_schedule(
        payload: ScheduleCreateRequest,
    ):
        from goliath.orchestration.scheduler import SchedulerService

        scheduler = SchedulerService(
            uow_factory=lambda: __import__(
                "goliath.orchestration.uow", fromlist=["SqlAlchemyJobUnitOfWork"]
            ).SqlAlchemyJobUnitOfWork(session_factory),
            orchestration_service=service,
            config=config,
        )
        return scheduler.create(
            name=payload.name,
            task_type=payload.task_type,
            objective_template=payload.objective_template,
            workspace=__import__("pathlib").Path(payload.workspace),
            timing_type=payload.timing_type,
            timing_value=payload.timing_value,
            timezone=payload.timezone,
            requested_agent=payload.requested_agent,
            permissions=payload.permissions,
            context_files=payload.context_files,
            owner="api",
        )

    @app.post(
        "/schedules/{schedule_id}/enable", dependencies=[Depends(require_scope("schedules:write"))]
    )
    async def enable_schedule(
        schedule_id: UUID,
    ):
        from goliath.db.operations import ScheduleRepository

        with session_factory() as session:
            value = ScheduleRepository(session).set_enabled(schedule_id, True)
            session.commit()
            return value

    @app.post(
        "/schedules/{schedule_id}/disable", dependencies=[Depends(require_scope("schedules:write"))]
    )
    async def disable_schedule(
        schedule_id: UUID,
    ):
        from goliath.db.operations import ScheduleRepository

        with session_factory() as session:
            value = ScheduleRepository(session).set_enabled(schedule_id, False)
            session.commit()
            return value

    @app.post(
        "/schedules/{schedule_id}/run-now", dependencies=[Depends(require_scope("schedules:write"))]
    )
    async def run_schedule_now(
        schedule_id: UUID,
    ):
        from pathlib import Path

        from goliath.db.operations import ScheduleRepository

        with session_factory() as session:
            value = ScheduleRepository(session).get(schedule_id)
            if value is None:
                raise HTTPException(status_code=404, detail="schedule not found")
            marketplace_schedule = value.task_type == "marketplace_operation"
            submission = (
                None
                if marketplace_schedule
                else JobSubmission(
                    task_type=value.task_type,
                    objective=value.objective_template,
                    workspace=Path(value.workspace_path),
                    requested_agent=value.requested_agent,
                    permissions=set(value.permissions),
                    context_files=[Path(path) for path in value.context_files],
                )
            )
        if marketplace_schedule:
            from goliath.orchestration.scheduler import SchedulerService

            scheduler = SchedulerService(
                uow_factory=lambda: __import__(
                    "goliath.orchestration.uow", fromlist=["SqlAlchemyJobUnitOfWork"]
                ).SqlAlchemyJobUnitOfWork(session_factory),
                orchestration_service=service,
                config=config,
            )
            return scheduler.run_now(schedule_id)
        assert submission is not None
        return await service.submit(submission)

    # ---------------------------------------------------------- domain routes
    def _domain_error(error: Exception) -> HTTPException:
        from goliath.db.repositories import (
            ForbiddenStatusError,
            VersionConflictError,
        )
        from goliath.db.repositories import (
            RecordNotFoundError as _NotFound,
        )

        if isinstance(error, _NotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, VersionConflictError):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, ForbiddenStatusError):
            return HTTPException(status_code=403, detail=str(error))
        return HTTPException(status_code=422, detail=str(error))

    def _item_dict(item):
        return {
            "id": str(item.id),
            "sku": item.sku,
            "title": item.title,
            "brand": item.brand,
            "model_name": item.model_name,
            "category": item.category,
            "status": item.status.value,
            "condition": item.condition.value,
            "size_label": item.size_label,
            "version": item.version,
            "created_at": item.created_at.isoformat(),
        }

    @app.get("/inventory", dependencies=[Depends(require_scope("inventory:read"))])
    async def list_inventory(
        status_filter: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        from goliath.db.models import InventoryStatus

        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        items = domain_service.list_inventory(
            status=InventoryStatus(status_filter) if status_filter else None,
            category=category,
            limit=limit,
            offset=offset,
        )
        return [_item_dict(item) for item in items]

    @app.post("/inventory", status_code=201)
    async def create_inventory(
        request: Request,
        payload: InventoryCreateRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("inventory:write_draft"))],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        from goliath.db.models import InventoryStatus

        if idempotency_key and current is None and config.api_authentication_required:
            authorization = request.headers.get("Authorization", "")
            if authorization.lower().startswith("bearer "):
                with session_factory() as auth_session:
                    authenticated = AuthRepository(auth_session).authenticate(authorization[7:])
                    if authenticated is not None:
                        current, _ = authenticated
                    auth_session.commit()

        fields = payload.model_dump()
        if idempotency_key and current is not None:
            request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
            with session_factory() as session:
                idem = IdempotencyRepository(session)
                existing = idem.get(current.id, idempotency_key)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise HTTPException(
                            status_code=409, detail="idempotency key payload conflict"
                        )
                    return _item_dict(domain_service.get_inventory(existing.resource_id))
        try:
            item = domain_service.create_inventory(
                actor="human:api",
                status=InventoryStatus(fields.pop("status")),
                **fields,
            )
        except Exception as error:
            raise _domain_error(error) from error
        if idempotency_key and current is not None:
            with session_factory() as session:
                IdempotencyRepository(session).create(
                    current.id,
                    idempotency_key,
                    hashlib.sha256(payload.model_dump_json().encode()).hexdigest(),
                    "inventory_item",
                    item.id,
                    datetime.now(UTC) + timedelta(seconds=config.idempotency_expiration_seconds),
                )
                session.commit()
        return _item_dict(item)

    @app.get("/inventory/{item_id}", dependencies=[Depends(require_scope("inventory:read"))])
    async def get_inventory(
        item_id: UUID,
    ):
        try:
            return _item_dict(domain_service.get_inventory(item_id))
        except Exception as error:
            raise _domain_error(error) from error

    @app.patch(
        "/inventory/{item_id}/draft", dependencies=[Depends(require_scope("inventory:write_draft"))]
    )
    async def update_inventory_draft(
        item_id: UUID,
        payload: InventoryDraftUpdateRequest,
    ):
        try:
            item = domain_service.update_inventory_draft(
                item_id,
                expected_version=payload.expected_version,
                changes=payload.changes,
                actor="human:api",
            )
            return _item_dict(item)
        except Exception as error:
            raise _domain_error(error) from error

    @app.get("/inventory/{item_id}/images", dependencies=[Depends(require_scope("inventory:read"))])
    async def inventory_images(
        item_id: UUID,
    ):
        return [
            {
                "id": str(m.id),
                "role": m.role.value,
                "media_type": m.media_type,
                "checksum": m.checksum,
            }
            for m in domain_service.list_media(item_id)
        ]

    @app.post(
        "/inventory/{item_id}/measurements",
        status_code=201,
        dependencies=[Depends(require_scope("inventory:write_draft"))],
    )
    async def add_measurement(
        item_id: UUID,
        payload: MeasurementCreateRequest,
    ):
        from decimal import Decimal

        from goliath.db.models import MeasurementUnit

        try:
            measurement = domain_service.add_measurement(
                item_id,
                actor="human:api",
                measurement_type=payload.measurement_type,
                value=Decimal(str(payload.value)),
                unit=MeasurementUnit(payload.unit),
                method=payload.method,
                confidence=Decimal(str(payload.confidence))
                if payload.confidence is not None
                else None,
                source=payload.source,
                notes=payload.notes,
            )
            return {"id": str(measurement.id), "value_cm": str(measurement.value_cm)}
        except Exception as error:
            raise _domain_error(error) from error

    @app.get(
        "/inventory/{item_id}/research", dependencies=[Depends(require_scope("inventory:read"))]
    )
    async def list_research(
        item_id: UUID,
    ):
        return [
            {"id": str(r.id), "status": r.status.value, "question": r.research_question}
            for r in domain_service.list_research_for_item(item_id)
        ]

    @app.post(
        "/inventory/{item_id}/research",
        status_code=201,
        dependencies=[Depends(require_scope("inventory:write_draft"))],
    )
    async def create_research(
        item_id: UUID,
        payload: ResearchCreateRequest,
    ):
        try:
            record = domain_service.create_research(
                item_id,
                actor="human:api",
                research_question=payload.research_question,
                researcher=payload.researcher,
                search_terms=payload.search_terms,
            )
            return {"id": str(record.id), "status": record.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    @app.get(
        "/inventory/{item_id}/comparables", dependencies=[Depends(require_scope("inventory:read"))]
    )
    async def list_comparables(
        item_id: UUID,
    ):
        return [
            {
                "id": str(c.id),
                "marketplace": c.marketplace.value,
                "is_sold": c.is_sold,
                "sold_price": str(c.sold_price) if c.sold_price is not None else None,
            }
            for c in domain_service.find_comparables(item_id)
        ]

    @app.post("/pricing/calculate", dependencies=[Depends(require_scope("pricing:calculate"))])
    async def pricing_calculate(
        payload: PricingCalculateRequest,
    ):
        from decimal import Decimal

        from goliath.domain.pricing import ComparableObservation, PricingInputs

        try:
            inputs = PricingInputs(
                cost_basis=Decimal(str(payload.cost_basis)),
                currency=payload.currency,
                condition=payload.condition,
                shipping_cost=Decimal(str(payload.shipping_cost)),
                is_stale=payload.is_stale,
                comparables=[
                    ComparableObservation(
                        price=Decimal(str(c["price"])),
                        is_sold=bool(c.get("is_sold", False)),
                        reliability=float(c.get("reliability", 0.5)),
                    )
                    for c in payload.comparables
                ],
            )
            result = domain_service.calculate_pricing(
                UUID(payload.item_id),
                actor="human:api",
                inputs=inputs,
                fee_version=payload.fee_version,
            )
            return result.model_dump(mode="json")
        except Exception as error:
            raise _domain_error(error) from error

    @app.get("/listing-drafts", dependencies=[Depends(require_scope("listing:read"))])
    async def list_listing_drafts(
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        from goliath.db.models import DraftStatus

        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        drafts = domain_service.list_drafts(
            status=DraftStatus(status_filter) if status_filter else None,
            limit=limit,
            offset=offset,
        )
        return [{"id": str(d.id), "status": d.status.value, "title": d.title} for d in drafts]

    @app.post(
        "/listing-drafts",
        status_code=201,
        dependencies=[Depends(require_scope("listing:write_draft"))],
    )
    async def create_listing_draft(
        payload: ListingDraftCreateRequest,
    ):
        try:
            draft = domain_service.create_master_draft(UUID(payload.item_id), actor="human:api")
            return {
                "id": str(draft.id),
                "status": draft.status.value,
                "validation_warnings": draft.validation_warnings,
                "missing_fields": draft.missing_fields,
            }
        except Exception as error:
            raise _domain_error(error) from error

    @app.get("/listing-drafts/{draft_id}", dependencies=[Depends(require_scope("listing:read"))])
    async def get_listing_draft(
        draft_id: UUID,
    ):
        try:
            draft = domain_service.get_draft(draft_id)
            return {
                "id": str(draft.id),
                "status": draft.status.value,
                "title": draft.title,
                "version": draft.version,
                "validation_warnings": draft.validation_warnings,
            }
        except Exception as error:
            raise _domain_error(error) from error

    @app.post(
        "/listing-drafts/{draft_id}/validate",
        dependencies=[Depends(require_scope("listing:write_draft"))],
    )
    async def validate_listing_draft(
        draft_id: UUID,
    ):
        try:
            return domain_service.validate_draft(draft_id, actor="human:api")
        except Exception as error:
            raise _domain_error(error) from error

    @app.post(
        "/listing-drafts/{draft_id}/request-approval",
        status_code=201,
        dependencies=[Depends(require_scope("listing:write_draft"))],
    )
    async def request_draft_approval(
        draft_id: UUID,
    ):
        try:
            proposal = domain_service.request_listing_approval(draft_id, actor="human:api")
            return {"id": str(proposal.id), "status": proposal.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    @app.get("/approvals", dependencies=[Depends(require_scope("approval:read"))])
    async def list_approvals(
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        from goliath.db.models import ProposalStatus

        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        proposals = domain_service.list_proposals(
            status=ProposalStatus(status_filter) if status_filter else None,
            limit=limit,
            offset=offset,
        )
        return [
            {
                "id": str(p.id),
                "type": p.proposal_type.value,
                "status": p.status.value,
                "resource_type": p.resource_type,
                "resource_id": str(p.resource_id),
                "risk_tier": p.risk_tier,
            }
            for p in proposals
        ]

    @app.get("/approvals/{approval_id}", dependencies=[Depends(require_scope("approval:read"))])
    async def get_approval(
        approval_id: UUID,
    ):
        try:
            p = domain_service.get_proposal(approval_id)
            return {
                "id": str(p.id),
                "type": p.proposal_type.value,
                "status": p.status.value,
                "current_version": p.current_version,
                "justification": p.justification,
            }
        except Exception as error:
            raise _domain_error(error) from error

    @app.post(
        "/approvals/{approval_id}/approve",
        dependencies=[Depends(require_scope("approval:request"))],
    )
    async def approve_approval(
        approval_id: UUID,
        payload: ApprovalDecisionRequest,
    ):
        try:
            p = domain_service.approve_proposal(
                approval_id, reviewer=payload.reviewer, notes=payload.notes
            )
            return {"id": str(p.id), "status": p.status.value, "execution_error": p.execution_error}
        except Exception as error:
            raise _domain_error(error) from error

    @app.post(
        "/approvals/{approval_id}/reject", dependencies=[Depends(require_scope("approval:request"))]
    )
    async def reject_approval(
        approval_id: UUID,
        payload: ApprovalDecisionRequest,
    ):
        try:
            p = domain_service.reject_proposal(
                approval_id, reviewer=payload.reviewer, reason=payload.reason
            )
            return {"id": str(p.id), "status": p.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    # ------------------------------------------------------------ media routes
    @app.post(
        "/media/{item_id}/upload",
        status_code=201,
        dependencies=[Depends(require_scope("media:write"))],
    )
    async def upload_media(
        item_id: UUID,
        file: UploadFile = File(...),  # noqa: B008
        role: str = Form("original"),
    ):
        from goliath.db.models import MediaRole

        data = await file.read()
        try:
            media = media_service.ingest_bytes(
                item_id,
                data,
                role=MediaRole(role),
                original_filename=file.filename,
                declared_media_type=file.content_type,
                actor="human:api",
            )
        except Exception as error:
            raise _domain_error(error) from error
        return {
            "id": str(media.id),
            "status": media.status.value,
            "checksum": media.checksum,
        }

    @app.get("/media/{item_id}", dependencies=[Depends(require_scope("media:read"))])
    async def list_media(
        item_id: UUID,
    ):
        return [
            {"id": str(m.id), "status": m.status.value, "role": m.role.value}
            for m in domain_service.list_media(item_id)
        ]

    @app.post("/media/{media_id}/process", dependencies=[Depends(require_scope("media:process"))])
    async def process_media(
        media_id: UUID,
    ):
        job_id = image_processing_service.enqueue(media_id)
        return {"job_id": str(job_id), "enqueued": True}

    @app.get("/media/quarantine/list", dependencies=[Depends(require_scope("media:read"))])
    async def quarantine_list():
        return [
            {"id": str(m.id), "findings": m.validation_result.get("findings", [])}
            for m in media_service.list_quarantined()
        ]

    @app.post("/media/{media_id}/archive", dependencies=[Depends(require_scope("media:write"))])
    async def archive_media(
        media_id: UUID,
    ):
        try:
            media = media_service.archive(media_id, actor="human:api")
            return {"id": str(media.id), "status": media.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    # ------------------------------------------------------- comparable routes
    @app.post(
        "/comparables/import",
        status_code=201,
        dependencies=[Depends(require_scope("comparables:write"))],
    )
    async def import_comparables(
        payload: ComparableImportRequest,
    ):
        from goliath.domain.comparables import (
            ComparableImportService,
            ImportError_,
            parse_csv,
            parse_json,
        )

        service_ = ComparableImportService(session_factory=session_factory, config=config)
        try:
            rows = (
                parse_csv(payload.content)
                if payload.source_format == "csv"
                else parse_json(payload.content)
            )
            return service_.import_rows(
                UUID(payload.item_id),
                rows,
                source_format=payload.source_format,
                actor="human:api",
                dry_run=payload.dry_run,
                all_or_nothing=payload.all_or_nothing,
            )
        except (ImportError_, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/comparables/review-list", dependencies=[Depends(require_scope("comparables:read"))])
    async def comparables_review_list(
        limit: int = 100,
        offset: int = 0,
    ):
        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        return [
            {
                "id": str(c.id),
                "marketplace": c.marketplace.value,
                "review_status": c.review_status.value,
                "is_sold": c.is_sold,
            }
            for c in review_service.list_comparables_pending(limit=limit, offset=offset)
        ]

    @app.post(
        "/comparables/{comparable_id}/accept",
        dependencies=[Depends(require_scope("comparables:review"))],
    )
    async def accept_comparable(
        comparable_id: UUID,
        payload: ComparableReviewRequest,
    ):
        return _comparable_decision(comparable_id, payload, "accepted")

    @app.post(
        "/comparables/{comparable_id}/reject",
        dependencies=[Depends(require_scope("comparables:review"))],
    )
    async def reject_comparable(
        comparable_id: UUID,
        payload: ComparableReviewRequest,
    ):
        return _comparable_decision(comparable_id, payload, "rejected")

    def _comparable_decision(comparable_id: UUID, payload: ComparableReviewRequest, decision: str):
        from decimal import Decimal

        from goliath.db.models import ComparableReviewStatus

        try:
            comparable = review_service.review_comparable(
                comparable_id,
                reviewer=payload.reviewer,
                decision=ComparableReviewStatus(decision),
                similarity_score=(
                    Decimal(str(payload.similarity_score))
                    if payload.similarity_score is not None
                    else None
                ),
                reliability_score=(
                    Decimal(str(payload.reliability_score))
                    if payload.reliability_score is not None
                    else None
                ),
                notes=payload.notes,
            )
            return {"id": str(comparable.id), "review_status": comparable.review_status.value}
        except Exception as error:
            raise _domain_error(error) from error

    # ----------------------------------------------------------- review routes
    @app.get("/reviews", dependencies=[Depends(require_scope("reviews:read"))])
    async def list_reviews(
        status_filter: str | None = None,
        task_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        from goliath.db.models import ReviewTaskStatus, ReviewTaskType

        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        tasks = review_service.list_tasks(
            status=ReviewTaskStatus(status_filter) if status_filter else None,
            task_type=ReviewTaskType(task_type) if task_type else None,
            limit=limit,
            offset=offset,
        )
        return [
            {
                "id": str(t.id),
                "task_type": t.task_type.value,
                "status": t.status.value,
                "priority": t.priority,
                "version": t.version,
                "reason": t.reason,
            }
            for t in tasks
        ]

    @app.post("/reviews/{task_id}/claim", dependencies=[Depends(require_scope("reviews:write"))])
    async def claim_review(
        task_id: UUID,
        payload: ReviewTaskDecisionRequest,
    ):
        try:
            task = review_service.get_task(task_id)
            version = (
                payload.expected_version if payload.expected_version is not None else task.version
            )
            claimed = review_service.claim_task(
                task_id, reviewer=payload.reviewer, expected_version=version
            )
            return {"id": str(claimed.id), "status": claimed.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    @app.post("/reviews/{task_id}/complete", dependencies=[Depends(require_scope("reviews:write"))])
    async def complete_review(
        task_id: UUID,
        payload: ReviewTaskDecisionRequest,
    ):
        try:
            task = review_service.complete_task(
                task_id, reviewer=payload.reviewer, outcome=payload.outcome, notes=payload.notes
            )
            return {"id": str(task.id), "status": task.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    @app.post("/reviews/{task_id}/dismiss", dependencies=[Depends(require_scope("reviews:write"))])
    async def dismiss_review(
        task_id: UUID,
        payload: ReviewTaskDecisionRequest,
    ):
        try:
            task = review_service.dismiss_task(
                task_id, reviewer=payload.reviewer, notes=payload.notes
            )
            return {"id": str(task.id), "status": task.status.value}
        except Exception as error:
            raise _domain_error(error) from error

    @app.post("/bulk/reviews/complete", dependencies=[Depends(require_scope("reviews:write"))])
    async def bulk_complete_reviews(
        payload: BulkRequest,
    ):
        try:
            return review_service.bulk_complete_tasks(
                [UUID(i) for i in payload.ids],
                reviewer=payload.reviewer,
                outcome=payload.outcome,
                all_or_nothing=payload.all_or_nothing,
            )
        except Exception as error:
            raise _domain_error(error) from error

    @app.post(
        "/bulk/comparables/review", dependencies=[Depends(require_scope("comparables:review"))]
    )
    async def bulk_review_comparables(
        payload: BulkRequest,
    ):
        from goliath.db.models import ComparableReviewStatus

        try:
            return review_service.bulk_review_comparables(
                [UUID(i) for i in payload.ids],
                reviewer=payload.reviewer,
                decision=ComparableReviewStatus(payload.decision or "accepted"),
                all_or_nothing=payload.all_or_nothing,
            )
        except Exception as error:
            raise _domain_error(error) from error

    # -------------------------------------------------------- dashboard routes
    @app.get("/dashboard/summary", dependencies=[Depends(require_scope("dashboard:read"))])
    async def dashboard_summary():
        return review_service.summary()

    @app.get("/dashboard/tasks", dependencies=[Depends(require_scope("dashboard:read"))])
    async def dashboard_tasks(
        limit: int = 100,
        offset: int = 0,
    ):
        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        return [
            {"id": str(t.id), "task_type": t.task_type.value, "status": t.status.value}
            for t in review_service.list_tasks(limit=limit, offset=offset)
        ]

    @app.get(
        "/dashboard/inventory-needing-review",
        dependencies=[Depends(require_scope("dashboard:read"))],
    )
    async def dashboard_inventory():
        return review_service.inventory_needing_review()

    @app.get(
        "/dashboard/research-needing-review",
        dependencies=[Depends(require_scope("dashboard:read"))],
    )
    async def dashboard_research():
        return review_service.research_needing_review()

    @app.get(
        "/dashboard/comparables-needing-review",
        dependencies=[Depends(require_scope("dashboard:read"))],
    )
    async def dashboard_comparables():
        return [
            {"id": str(c.id), "marketplace": c.marketplace.value}
            for c in review_service.list_comparables_pending()
        ]

    @app.get(
        "/dashboard/listings-needing-review",
        dependencies=[Depends(require_scope("dashboard:read"))],
    )
    async def dashboard_listings():
        return review_service.listings_needing_review()

    @app.get("/dashboard/approvals", dependencies=[Depends(require_scope("dashboard:read"))])
    async def dashboard_approvals():
        return review_service.approvals()

    @app.get("/dashboard/media-failures", dependencies=[Depends(require_scope("dashboard:read"))])
    async def dashboard_media_failures():
        return review_service.media_failures()

    @app.get("/dashboard/worker-health", dependencies=[Depends(require_scope("dashboard:read"))])
    async def dashboard_worker_health():
        return review_service.worker_health()

    # ------------------------------------------------------ marketplace routes
    def _marketplace_error(error: Exception) -> HTTPException:
        from goliath.db.repositories import RecordNotFoundError as _NF
        from goliath.db.repositories import VersionConflictError as _VC
        from goliath.marketplace.broker import SessionBrokerError
        from goliath.marketplace.ebay_auth import EbayOAuthError
        from goliath.marketplace.gateway import GatewayError
        from goliath.marketplace.service import MarketplaceServiceError

        if isinstance(error, _NF):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, _VC):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, GatewayError):
            return HTTPException(status_code=403, detail=str(error))
        if isinstance(error, MarketplaceServiceError | ValueError):
            return HTTPException(status_code=422, detail=str(error))
        if isinstance(error, EbayOAuthError | SessionBrokerError):
            return HTTPException(status_code=422, detail=str(error))
        raise error

    def _scopes(current: ApiPrincipal | None) -> set[str] | None:
        return set(current.scopes) if current is not None else None

    def _safe_marketplace_data(operation: str, data: dict) -> dict:
        allowed = {
            "status",
            "remote_listing_id",
            "id",
            "title",
            "price",
            "currency",
            "tracking_number",
            "carrier",
            "refreshed",
            "promoted",
            "shared",
        }
        return {key: data[key] for key in allowed if key in data}

    def _receipt_json(receipt) -> dict:
        return {
            "ok": receipt.ok,
            "executed": receipt.executed,
            "shadowed": receipt.shadowed,
            "verified": receipt.verified,
            "operation": receipt.operation,
            "error_category": receipt.error_category,
            "reasons": receipt.reasons,
            "data": _safe_marketplace_data(receipt.operation, receipt.data),
        }

    @app.get("/automation/status", dependencies=[Depends(require_scope("marketplace:read"))])
    async def automation_status():
        return marketplace_service.automation_status()

    @app.post("/automation/stop", dependencies=[Depends(require_scope("admin"))])
    async def automation_stop(
        payload: AutomationCommandRequest,
    ):
        return marketplace_service.emergency_stop(
            reason=payload.reason, scope_key=payload.scope_key
        )

    @app.post("/automation/start", dependencies=[Depends(require_scope("admin"))])
    async def automation_start(
        payload: AutomationCommandRequest,
    ):
        try:
            return marketplace_service.emergency_start(
                reason=payload.reason, scope_key=payload.scope_key
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get("/marketplace-accounts", dependencies=[Depends(require_scope("marketplace:read"))])
    async def list_accounts():
        return [
            {
                "id": str(a.id),
                "marketplace": a.marketplace.value,
                "label": a.account_label,
                "mode": a.automation_mode.value,
                "status": a.status.value,
                "version": a.version,
            }
            for a in marketplace_service.list_accounts()
        ]

    @app.post("/marketplace-accounts/connect", dependencies=[Depends(require_scope("admin"))])
    async def connect_marketplace_account(payload: MarketplaceConnectRequest):
        try:
            return marketplace_service.connect_account(payload.account_id, sandbox=payload.sandbox)
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-accounts/oauth/ebay/callback")
    async def ebay_oauth_callback(payload: OAuthCallbackRequest):
        try:
            return await marketplace_service.complete_account_connection(
                code=payload.code, state=payload.state, sandbox=payload.sandbox
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post(
        "/marketplace-accounts", status_code=201, dependencies=[Depends(require_scope("admin"))]
    )
    async def create_account(
        payload: MarketplaceAccountCreateRequest,
    ):
        try:
            account = marketplace_service.create_account(
                marketplace=payload.marketplace,
                label=payload.label,
                currency=payload.currency,
                capabilities=payload.capabilities,
            )
            return {"id": str(account.id), "status": account.status.value}
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get(
        "/marketplace-accounts/{account_id}",
        dependencies=[Depends(require_scope("marketplace:read"))],
    )
    async def get_account(
        account_id: UUID,
    ):
        try:
            a = marketplace_service.get_account(account_id)
            return {
                "id": str(a.id),
                "marketplace": a.marketplace.value,
                "mode": a.automation_mode.value,
                "status": a.status.value,
                "consecutive_failures": a.consecutive_failures,
                "version": a.version,
            }
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get(
        "/marketplace-accounts/{account_id}/connection",
        dependencies=[Depends(require_scope("marketplace:read"))],
    )
    async def marketplace_account_connection(account_id: UUID):
        try:
            return marketplace_service.account_connection(account_id)
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get(
        "/marketplace-accounts/{account_id}/capabilities",
        dependencies=[Depends(require_scope("marketplace:read"))],
    )
    async def marketplace_account_capabilities(account_id: UUID):
        try:
            return marketplace_service.capabilities(account_id)
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-accounts/{account_id}/reauthenticate")
    async def reauthenticate_marketplace_account(
        account_id: UUID,
        payload: MarketplaceReauthenticateRequest,
        _current: Annotated[ApiPrincipal, Depends(require_scope("admin"))],
    ):
        try:
            return await marketplace_service.reauthenticate_account(
                account_id, sandbox=payload.sandbox
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-accounts/{account_id}/disconnect")
    async def disconnect_marketplace_account(
        account_id: UUID,
        _current: Annotated[ApiPrincipal, Depends(require_scope("admin"))],
    ):
        try:
            return await marketplace_service.disconnect_account(account_id)
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-accounts/{account_id}/validate")
    async def validate_marketplace_account(
        account_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:read"))],
    ):
        try:
            return _receipt_json(
                await marketplace_service.validate_account(
                    account_id, principal_scopes=_scopes(current)
                )
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post(
        "/marketplace-accounts/{account_id}/authenticate",
        dependencies=[Depends(require_scope("admin"))],
    )
    async def authenticate_account(
        account_id: UUID,
        payload: AuthenticateRequest,
    ):
        import base64

        try:
            state = base64.b64decode(payload.session_state_b64)
            marketplace_service.authenticate_account(account_id, state)
            return {"authenticated": True}
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post(
        "/marketplace-accounts/{account_id}/mode", dependencies=[Depends(require_scope("admin"))]
    )
    async def set_account_mode(
        account_id: UUID,
        payload: ModeChangeRequest,
    ):
        try:
            account = marketplace_service.set_mode(
                account_id, mode=payload.mode, expected_version=payload.expected_version
            )
            return {"id": str(account.id), "mode": account.automation_mode.value}
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-accounts/{account_id}/sync")
    async def sync_account(
        account_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:sync"))],
    ):
        try:
            return await marketplace_service.sync_account(
                account_id, principal_scopes=_scopes(current), actor="api:principal"
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get("/marketplace-listings", dependencies=[Depends(require_scope("marketplace:read"))])
    async def list_listings(
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        from goliath.db.models import RemoteListingStatus

        if limit < 1 or limit > 1_000 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        listings = marketplace_service.list_listings(
            status=RemoteListingStatus(status_filter) if status_filter else None,
            limit=limit,
            offset=offset,
        )
        return [
            {
                "id": str(x.id),
                "status": x.status.value,
                "remote_listing_id": x.remote_listing_id,
                "account_id": str(x.account_id),
            }
            for x in listings
        ]

    @app.get("/remote-listings", dependencies=[Depends(require_scope("marketplace:read"))])
    async def remote_listings_alias(
        status_filter: str | None = None, limit: int = 100, offset: int = 0
    ):
        return await list_listings(status_filter=status_filter, limit=limit, offset=offset)

    @app.get(
        "/remote-listings/{listing_id}",
        dependencies=[Depends(require_scope("marketplace:read"))],
    )
    async def get_remote_listing(listing_id: UUID):
        from goliath.db.marketplace_repositories import RemoteListingRepository

        with session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise HTTPException(status_code=404, detail="remote listing not found")
            return {
                "id": str(listing.id),
                "account_id": str(listing.account_id),
                "inventory_item_id": str(listing.inventory_item_id),
                "remote_listing_id": listing.remote_listing_id,
                "status": listing.status.value,
                "sync_state": listing.sync_state.value,
                "price": str(listing.current_price) if listing.current_price is not None else None,
                "currency": listing.currency,
                "quantity": listing.quantity,
                "last_synced_at": listing.last_synced_at,
            }

    @app.get(
        "/marketplace-operations/{operation_id}",
        dependencies=[Depends(require_scope("marketplace:read"))],
    )
    async def get_marketplace_operation(operation_id: UUID):
        from goliath.db.marketplace_repositories import OperationAttemptRepository

        with session_factory() as session:
            operation = OperationAttemptRepository(session).get(operation_id)
            if operation is None:
                raise HTTPException(status_code=404, detail="marketplace operation not found")
            return {
                "id": str(operation.id),
                "account_id": str(operation.account_id),
                "operation": operation.operation,
                "status": operation.status.value,
                "verification_status": operation.verification_status.value,
                "remote_identifier": operation.remote_identifier,
                "attempt_count": operation.attempt_count,
                "created_at": operation.created_at,
                "completed_at": operation.completed_at,
                "error_category": operation.error_category,
            }

    @app.post("/marketplace-listings/publish", status_code=201)
    async def publish_listing(
        payload: PublishRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:create"))],
    ):
        try:
            return await marketplace_service.publish_item(
                UUID(payload.item_id),
                account_ids=[UUID(a) for a in payload.account_ids] if payload.account_ids else None,
                principal_scopes=_scopes(current),
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/marketplace-listings/{listing_id}/refresh")
    async def refresh_listing(
        listing_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:refresh"))],
    ):
        receipt = await marketplace_service.refresh_listing(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )
        return _receipt_json(receipt)

    @app.post("/marketplace-listings/{listing_id}/promote")
    async def promote_listing(
        listing_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:promote"))],
    ):
        receipt = await marketplace_service.promote_listing(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )
        return _receipt_json(receipt)

    @app.post("/marketplace-listings/{listing_id}/share")
    async def share_listing(
        listing_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:share"))],
    ):
        receipt = await marketplace_service.share_listing(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )
        return {
            "ok": receipt.ok,
            "executed": receipt.executed,
            "shadowed": receipt.shadowed,
            "verified": receipt.verified,
            "operation": receipt.operation,
            "error_category": receipt.error_category,
        }

    @app.post("/marketplace-listings/{listing_id}/update")
    async def update_listing(
        listing_id: UUID,
        payload: ListingUpdateRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:update"))],
    ):
        if payload.fields:
            receipt = await marketplace_service.update_listing(
                listing_id,
                fields=payload.fields,
                principal_scopes=_scopes(current),
                actor="api:principal",
            )
            return _receipt_json(receipt)
        return await marketplace_service.run_price_automation(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )

    @app.post("/marketplace-listings/{listing_id}/sync")
    async def sync_listing(
        listing_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:sync"))],
    ):
        receipt = await marketplace_service.read_listing(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )
        return {
            "ok": receipt.ok,
            "executed": receipt.executed,
            "shadowed": receipt.shadowed,
            "verified": receipt.verified,
            "operation": receipt.operation,
            "data": _safe_marketplace_data(receipt.operation, receipt.data),
            "error_category": receipt.error_category,
        }

    @app.post("/marketplace-listings/{listing_id}/end")
    async def end_listing(
        listing_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:listing:end"))],
    ):
        receipt = await marketplace_service.end_listing(
            listing_id, principal_scopes=_scopes(current), actor="api:principal"
        )
        return _receipt_json(receipt)

    @app.get("/orders", dependencies=[Depends(require_scope("marketplace:order:read"))])
    async def list_orders():
        return [
            {"id": str(o.id), "status": o.status.value, "sale_price": str(o.sale_price)}
            for o in marketplace_service.list_orders()
        ]

    @app.get("/offers", dependencies=[Depends(require_scope("marketplace:offer:read"))])
    async def list_offers():
        return [
            {"id": str(o.id), "status": o.status.value, "offer_amount": str(o.offer_amount)}
            for o in marketplace_service.list_offers()
        ]

    @app.post("/orders/sync")
    async def sync_orders(
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:sync"))],
    ):
        return [
            await marketplace_service.sync_account(
                account.id, principal_scopes=_scopes(current), actor="api:principal"
            )
            for account in marketplace_service.list_accounts()
        ]

    @app.post("/offers/{offer_id}/evaluate")
    async def evaluate_offer(
        offer_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:offer:respond"))],
    ):
        try:
            return await marketplace_service.handle_offer(
                offer_id, principal_scopes=_scopes(current), actor="api:principal"
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/offers/{offer_id}/execute")
    async def execute_offer(
        offer_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:offer:respond"))],
    ):
        try:
            return await marketplace_service.handle_offer(
                offer_id, principal_scopes=_scopes(current), actor="api:principal"
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get("/messages", dependencies=[Depends(require_scope("marketplace:message:read"))])
    async def list_messages():
        from goliath.db.marketplace_repositories import MessageRepository

        with session_factory() as session:
            threads = MessageRepository(session).list_threads()
            return [
                {"id": str(t.id), "escalated": t.escalated, "last_category": t.last_category}
                for t in threads
            ]

    @app.post("/messages/{thread_id}/respond")
    async def respond_message(
        thread_id: UUID,
        payload: MessageRespondRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:message:routine"))],
    ):
        try:
            return await marketplace_service.respond_message(
                thread_id,
                payload.body,
                facts=payload.facts,
                principal_scopes=_scopes(current),
                actor="api:principal",
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get("/shipping/tasks", dependencies=[Depends(require_scope("marketplace:read"))])
    async def list_shipping():
        from goliath.db.marketplace_repositories import ShippingTaskRepository

        with session_factory() as session:
            return [
                {"id": str(t.id), "status": t.status.value, "profile": t.package_profile}
                for t in ShippingTaskRepository(session).list()
            ]

    @app.post("/shipping/tasks/{task_id}/purchase-label")
    async def purchase_label(
        task_id: UUID,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:label:purchase"))],
    ):
        try:
            return await marketplace_service.purchase_label(
                task_id, principal_scopes=_scopes(current), actor="api:principal"
            )
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.post("/shipping/tasks/{task_id}/confirm")
    async def confirm_shipment(
        task_id: UUID,
        payload: TrackingUpdateRequest,
        current: Annotated[ApiPrincipal, Depends(require_scope("marketplace:tracking:update"))],
    ):
        from goliath.db.marketplace_repositories import ShippingTaskRepository
        from goliath.db.models import ShippingTaskStatus

        with session_factory() as session:
            task = ShippingTaskRepository(session).get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="shipping task not found")
            order_id = task.order_id
            version = task.version
        receipt = await marketplace_service.update_tracking(
            order_id,
            payload.tracking_number,
            payload.carrier,
            principal_scopes=_scopes(current),
            actor="api:principal",
        )
        if not receipt.ok:
            return {"ok": False, "error_category": receipt.error_category}
        with session_factory() as session:
            ShippingTaskRepository(session).update(
                task_id, expected_version=version, status=ShippingTaskStatus.SHIPPED
            )
            session.commit()
        return {"ok": True, "verified": receipt.verified}

    @app.get("/reconciliation/issues", dependencies=[Depends(require_scope("marketplace:read"))])
    async def reconciliation_issues():
        from goliath.db.marketplace_repositories import ReconciliationRepository

        with session_factory() as session:
            return [
                {"id": str(r.id), "order_id": str(r.order_id), "discrepancies": r.discrepancies}
                for r in ReconciliationRepository(session).list_discrepancies()
            ]

    @app.post("/reconciliation/run", dependencies=[Depends(require_scope("admin"))])
    async def reconciliation_run():
        return marketplace_service.run_reconciliation()

    @app.get("/circuit-breakers", dependencies=[Depends(require_scope("marketplace:read"))])
    async def list_breakers():
        return [
            {"id": str(b.id), "scope": b.scope, "scope_key": b.scope_key, "state": b.state.value}
            for b in marketplace_service.list_breakers()
        ]

    @app.post(
        "/circuit-breakers/{breaker_id}/reset", dependencies=[Depends(require_scope("admin"))]
    )
    async def reset_breaker(
        breaker_id: UUID,
    ):
        try:
            b = marketplace_service.reset_breaker(breaker_id)
            return {"id": str(b.id), "state": b.state.value}
        except Exception as error:
            raise _marketplace_error(error) from error

    @app.get("/metrics", dependencies=[Depends(require_scope("admin"))])
    async def metrics():
        if not config.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics disabled")
        with session_factory() as session:
            jobs = list(session.scalars(select(AgentJobRecord.status)).all())
            workers = list(session.scalars(select(WorkerRecord.status)).all())
        counts = {status.value: jobs.count(status) for status in AgentJobStatus}
        lines = ["# HELP goliath_jobs Jobs by status", "# TYPE goliath_jobs gauge"]
        lines.extend(f'goliath_jobs{{status="{name}"}} {count}' for name, count in counts.items())
        lines.append(f'goliath_workers{{status="healthy"}} {workers.count(WorkerStatus.HEALTHY)}')
        lines.extend(_domain_metric_lines(session_factory))
        lines.append("goliath_up 1")
        return "\n".join(lines) + "\n"

    return app


def _domain_metric_lines(session_factory) -> list[str]:
    """Domain metrics; labels are limited to statuses/types/tools (never titles or PII)."""
    from goliath.db.models import (
        AuditEvent,
        ComparableSale,
        DomainProposal,
        InventoryItem,
        InventoryStatus,
        MasterListingDraft,
        ProposalStatus,
        ResearchRecord,
        ResearchStatus,
    )

    lines: list[str] = []
    with session_factory() as session:
        inv = list(session.scalars(select(InventoryItem.status)).all())
        research = list(session.scalars(select(ResearchRecord.status)).all())
        drafts = list(session.scalars(select(MasterListingDraft.status)).all())
        proposals = list(session.scalars(select(DomainProposal.status)).all())
        comparables = session.scalar(select(func.count()).select_from(ComparableSale)) or 0
        pricing_calcs = (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "pricing.calculated")
            )
            or 0
        )
        mcp_events = list(
            session.scalars(
                select(AuditEvent.details).where(AuditEvent.event_type == "mcp.request")
            ).all()
        )
        mcp_authz_failures = (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "mcp.authorization_failed")
            )
            or 0
        )
        stale_rejections = (
            session.scalar(
                select(func.count())
                .select_from(DomainProposal)
                .where(DomainProposal.status == ProposalStatus.EXECUTION_FAILED)
            )
            or 0
        )

    for inv_status in InventoryStatus:
        lines.append(
            f'goliath_inventory_items{{status="{inv_status.value}"}} {inv.count(inv_status)}'
        )
    for res_status in ResearchStatus:
        lines.append(
            f'goliath_research{{status="{res_status.value}"}} {research.count(res_status)}'
        )
    lines.append(f"goliath_research_inconclusive {research.count(ResearchStatus.INCONCLUSIVE)}")
    from goliath.db.models import DraftStatus

    for draft_status in DraftStatus:
        lines.append(
            f'goliath_listing_drafts{{status="{draft_status.value}"}} {drafts.count(draft_status)}'
        )
    for prop_status in ProposalStatus:
        lines.append(
            f'goliath_proposals{{status="{prop_status.value}"}} {proposals.count(prop_status)}'
        )
    lines.append(f"goliath_approval_queue_size {proposals.count(ProposalStatus.PENDING)}")
    lines.append(f"goliath_comparables_total {comparables}")
    lines.append(f"goliath_pricing_calculations_total {pricing_calcs}")
    lines.append(f"goliath_stale_proposal_rejections_total {stale_rejections}")
    lines.append(f"goliath_mcp_authorization_failures_total {mcp_authz_failures}")
    tool_counts: dict[str, int] = {}
    for detail in mcp_events:
        tool = (detail or {}).get("tool", "unknown")
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
    for tool, count in sorted(tool_counts.items()):
        lines.append(f'goliath_mcp_requests{{tool="{tool}"}} {count}')
    lines.extend(_milestone_five_metric_lines(session_factory))
    lines.extend(_milestone_six_metric_lines(session_factory))
    return lines


def _milestone_six_metric_lines(session_factory) -> list[str]:
    """Marketplace metrics. Labels are only statuses/operations — never titles/URLs/ids."""
    from goliath.db.models import (
        AuditEvent,
        CircuitBreaker,
        CircuitBreakerState,
        ExceptionStatus,
        ExceptionTask,
        MarketplaceOffer,
        MarketplaceOperationAttempt,
        MarketplaceOrder,
        OperationStatus,
        RemoteListing,
        RemoteListingStatus,
        ShippingTask,
    )

    def _event_count(session, event_type: str) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == event_type)
            )
            or 0
        )

    lines: list[str] = []
    with session_factory() as session:
        listing_status = list(session.scalars(select(RemoteListing.status)).all())
        op_status = list(session.scalars(select(MarketplaceOperationAttempt.status)).all())
        operation_rows = list(
            session.execute(
                select(MarketplaceOperationAttempt.operation, MarketplaceOperationAttempt.status)
            ).all()
        )
        orders = session.scalar(select(func.count()).select_from(MarketplaceOrder)) or 0
        open_exceptions = (
            session.scalar(
                select(func.count())
                .select_from(ExceptionTask)
                .where(ExceptionTask.status == ExceptionStatus.OPEN)
            )
            or 0
        )
        open_breakers = (
            session.scalar(
                select(func.count())
                .select_from(CircuitBreaker)
                .where(CircuitBreaker.state == CircuitBreakerState.OPEN)
            )
            or 0
        )
        published = _event_count(session, "listing.published")
        refreshed = _event_count(session, "listing.refreshed")
        ended = _event_count(session, "delisting.verified")
        delist_failures = _event_count(session, "delisting.failed")
        price_changes = _event_count(session, "price.changed")
        labels = _event_count(session, "label.purchased")
        emergency_stops = _event_count(session, "emergency_stop.activated")
        breaker_openings = _event_count(session, "breaker.opened")
        event_metrics = {
            "goliath_authentication_failures_total": "session.authentication_failed",
            "goliath_rate_limit_events_total": "marketplace.rate_limited",
            "goliath_listings_promoted_total": "listing.promoted",
            "goliath_publish_failures_total": "marketplace.create_listing.failed",
            "goliath_verification_failures_total": "marketplace.verification_failed",
            "goliath_synchronization_conflicts_total": "synchronization.conflict",
            "goliath_duplicate_sale_risks_total": "duplicate_sale.risk",
            "goliath_messages_sent_total": "buyer.response_sent",
            "goliath_message_escalations_total": "message.escalated",
            "goliath_relists_total": "relist.executed",
            "goliath_shipping_deadlines_missed_total": "shipping.deadline_missed",
            "goliath_reconciliation_discrepancies_total": "discrepancy.detected",
            "goliath_refunds_total": "refund.issued",
            "goliath_autonomous_actions_total": "marketplace.autonomous_action",
            "goliath_exception_tasks_total": "exception.created",
        }
        event_values = {
            metric: _event_count(session, event) for metric, event in event_metrics.items()
        }
        offers = list(session.scalars(select(MarketplaceOffer.status)).all())
        label_cost = session.scalar(select(func.sum(ShippingTask.label_cost))) or 0

    for listing_state in RemoteListingStatus:
        lines.append(
            f'goliath_remote_listings{{status="{listing_state.value}"}} '
            f"{listing_status.count(listing_state)}"
        )
    from goliath.marketplace.gateway import WRITE_OPERATIONS

    completed = {OperationStatus.SUCCEEDED, OperationStatus.VERIFIED}
    write_count = sum(
        1
        for operation, state in operation_rows
        if operation in WRITE_OPERATIONS and state in completed
    )
    read_count = sum(
        1
        for operation, state in operation_rows
        if operation not in WRITE_OPERATIONS and state in completed
    )
    lines.append(f"goliath_marketplace_reads {read_count}")
    lines.append(f"goliath_marketplace_writes {write_count}")
    lines.append(f"goliath_marketplace_write_failures {op_status.count(OperationStatus.FAILED)}")
    lines.append(f"goliath_marketplace_shadow_writes {op_status.count(OperationStatus.SHADOWED)}")
    lines.append(f"goliath_orders_detected_total {orders}")
    lines.append(f"goliath_listings_published_total {published}")
    lines.append(f"goliath_listings_refreshed_total {refreshed}")
    lines.append(f"goliath_delisting_verified_total {ended}")
    lines.append(f"goliath_delisting_failures_total {delist_failures}")
    lines.append(f"goliath_price_changes_total {price_changes}")
    lines.append(f"goliath_labels_purchased_total {labels}")
    lines.append(f"goliath_emergency_stops_total {emergency_stops}")
    lines.append(f"goliath_circuit_breaker_openings_total {breaker_openings}")
    lines.append(f"goliath_open_exceptions {open_exceptions}")
    lines.append(f"goliath_open_circuit_breakers {open_breakers}")
    from goliath.db.models import OfferStatus

    lines.append(f"goliath_offers_accepted_total {offers.count(OfferStatus.ACCEPTED)}")
    lines.append(f"goliath_offers_declined_total {offers.count(OfferStatus.DECLINED)}")
    lines.append(f"goliath_offers_countered_total {offers.count(OfferStatus.COUNTERED)}")
    lines.append(f"goliath_label_cost_total {label_cost}")
    for metric, value in sorted(event_values.items()):
        lines.append(f"{metric} {value}")
    return lines


def _milestone_five_metric_lines(session_factory) -> list[str]:
    """Media, comparable, and review metrics. Labels are only statuses/types/tools."""
    from goliath.db.models import (
        AuditEvent,
        ComparableImport,
        ComparableReviewStatus,
        ComparableSale,
        ImageQualityResult,
        InventoryMedia,
        MediaJobStatus,
        MediaProcessingJob,
        MediaStatus,
        ReviewTask,
        ReviewTaskStatus,
    )

    lines: list[str] = []
    with session_factory() as session:
        media_status = list(session.scalars(select(InventoryMedia.status)).all())
        job_status = list(session.scalars(select(MediaProcessingJob.status)).all())
        review_status = list(session.scalars(select(ReviewTask.status)).all())
        comp_review = list(session.scalars(select(ComparableSale.review_status)).all())
        imports = session.scalar(select(func.count()).select_from(ComparableImport)) or 0
        quality_warnings = (
            session.scalar(
                select(func.count())
                .select_from(ImageQualityResult)
                .where(ImageQualityResult.passed.is_(False))
            )
            or 0
        )
        thumbnails = (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "media.processed")
            )
            or 0
        )

    for media_state in MediaStatus:
        lines.append(
            f'goliath_media{{status="{media_state.value}"}} {media_status.count(media_state)}'
        )
    for job_state in MediaJobStatus:
        lines.append(
            f'goliath_media_jobs{{status="{job_state.value}"}} {job_status.count(job_state)}'
        )
    for review_state in ReviewTaskStatus:
        lines.append(
            f'goliath_review_tasks{{status="{review_state.value}"}} '
            f"{review_status.count(review_state)}"
        )
    lines.append(
        "goliath_comparables_awaiting_review "
        f"{comp_review.count(ComparableReviewStatus.PENDING_REVIEW)}"
    )
    lines.append(
        f"goliath_comparables_accepted {comp_review.count(ComparableReviewStatus.ACCEPTED)}"
    )
    lines.append(
        f"goliath_comparables_rejected {comp_review.count(ComparableReviewStatus.REJECTED)}"
    )
    lines.append(f"goliath_comparable_imports_total {imports}")
    lines.append(f"goliath_image_quality_warnings_total {quality_warnings}")
    lines.append(f"goliath_media_processed_total {thumbnails}")
    return lines
