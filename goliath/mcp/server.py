"""Goliath MCP server: bounded resale-domain and marketplace operation tools.

The server exposes only safe, typed tools. It never exposes raw database
sessions, unrestricted SQL, shell execution, credentials, marketplace tokens,
or unrestricted live-mutation controls. Every call authenticates a service principal, enforces
a scope, appends an audit event, and returns controlled errors.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.domain_repositories import McpPrincipalRepository
from goliath.db.marketplace_repositories import (
    MarketplaceOfferRepository,
    MarketplaceOrderRepository,
    MessageRepository,
    RemoteListingRepository,
    ShippingTaskRepository,
)
from goliath.db.models import (
    Marketplace,
    McpServicePrincipal,
    ProposalStatus,
    ProposalType,
    ResearchStatus,
    SourceReliability,
)
from goliath.db.repositories import (
    AuditEventRepository,
    InvalidStateTransitionError,
    RecordNotFoundError,
    VersionConflictError,
)
from goliath.domain.pricing import ComparableObservation, PricingInputs
from goliath.domain.service import DomainError, DomainService
from goliath.marketplace.broker import SessionBrokerError
from goliath.marketplace.gateway import GatewayError
from goliath.marketplace.service import MarketplaceServiceError

# Tools that must never exist on the Goliath MCP surface.
FORBIDDEN_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "execute_sql",
        "run_shell",
        "get_secret",
        "get_marketplace_token",
        "publish_listing",
        "change_live_price",
        "send_buyer_message",
        "issue_refund",
        "purchase_label",
    }
)


class McpError(RuntimeError):
    """Base for controlled MCP errors (never carries a stack trace to the caller)."""


class McpAuthenticationError(McpError):
    pass


class McpAuthorizationError(McpError):
    pass


class McpToolError(McpError):
    pass


class McpUnknownToolError(McpError):
    pass


@dataclass(slots=True)
class _Tool:
    scope: str
    handler: Callable[[GoliathMcpServer, McpServicePrincipal, dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class McpMetrics:
    requests_by_tool: Counter[str] = field(default_factory=Counter)
    authorization_failures: int = 0
    authentication_failures: int = 0


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise McpToolError(f"invalid decimal for {field_name}: {value}") from error


def _uuid(value: Any, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as error:
        raise McpToolError(f"invalid id for {field_name}: {value}") from error


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] is None:
        raise McpToolError(f"missing required argument: {key}")
    return payload[key]


class GoliathMcpServer:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._service = DomainService(session_factory=session_factory, config=config)
        from goliath.marketplace.service import MarketplaceService

        self._marketplace = MarketplaceService(session_factory=session_factory, config=config)
        self.metrics = McpMetrics()
        self._tools = self._build_tools()
        # Defensive: guarantee no forbidden tool is ever registered.
        overlap = set(self._tools) & FORBIDDEN_MCP_TOOLS
        if overlap:
            raise RuntimeError(f"forbidden MCP tools present: {sorted(overlap)}")

    # ---------------------------------------------------------------- public
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def tool_scopes(self) -> dict[str, str]:
        return {name: tool.scope for name, tool in self._tools.items()}

    def authenticate(self, credential: str) -> McpServicePrincipal:
        if not self._config.domain.mcp_authentication_required:
            raise McpAuthenticationError("MCP authentication is required by configuration")
        with self._session_factory() as session:
            principal = McpPrincipalRepository(session).authenticate(credential)
            if principal is None:
                self.metrics.authentication_failures += 1
                raise McpAuthenticationError("invalid or expired MCP credential")
            session.expunge(principal)
            session.commit()
            return principal

    def call(self, tool_name: str, *, credential: str, arguments: dict[str, Any] | None = None):
        arguments = arguments or {}
        if tool_name in FORBIDDEN_MCP_TOOLS:
            raise McpUnknownToolError(f"tool is not available: {tool_name}")
        tool = self._tools.get(tool_name)
        if tool is None:
            raise McpUnknownToolError(f"unknown tool: {tool_name}")
        principal = self.authenticate(credential)
        scopes = set(principal.scopes)
        is_marketplace = tool_name.startswith("marketplace.")
        has_scope = tool.scope in scopes or "admin" in scopes
        if is_marketplace and not has_scope:
            # Account-scoped marketplace grants are checked against the actual
            # resource below; a prefix match alone is never authorization.
            account_ids = self._marketplace_account_ids(tool_name, arguments)
            scoped = {
                candidate for candidate in scopes if candidate.startswith(tool.scope + "@")
            }
            has_scope = bool(account_ids) and all(
                f"{tool.scope}@{account_id}" in scoped for account_id in account_ids
            )
        if not has_scope:
            self.metrics.authorization_failures += 1
            self._audit_request(principal, tool_name, authorized=False)
            raise McpAuthorizationError(f"scope required: {tool.scope}")
        self.metrics.requests_by_tool[tool_name] += 1
        self._audit_request(principal, tool_name, authorized=True)
        try:
            return tool.handler(self, principal, arguments)
        except (
            DomainError,
            RecordNotFoundError,
            InvalidStateTransitionError,
            VersionConflictError,
            GatewayError,
            SessionBrokerError,
            MarketplaceServiceError,
            ValueError,
        ) as error:
            raise McpToolError(str(error)) from error

    def _marketplace_account_ids(self, tool_name: str, payload: dict[str, Any]) -> set[str]:
        """Resolve marketplace resource ownership before invoking a handler."""
        if "account_id" in payload:
            return {str(payload["account_id"])}
        if "account_ids" in payload:
            values = {str(value) for value in payload.get("account_ids", [])}
            if values:
                return values
            # A scoped principal cannot safely select every account implicitly.
            return set()
        resource_id = next(
            (payload.get(key) for key in ("listing_id", "offer_id", "order_id", "thread_id", "task_id") if payload.get(key)),
            None,
        )
        if resource_id is None:
            return set()
        with self._session_factory() as session:
            if "listing_id" in payload:
                row = RemoteListingRepository(session).get(_uuid(resource_id, "listing_id"))
                return {str(row.account_id)} if row else set()
            if "offer_id" in payload:
                row = MarketplaceOfferRepository(session).get(_uuid(resource_id, "offer_id"))
                return {str(row.account_id)} if row else set()
            if "order_id" in payload:
                row = MarketplaceOrderRepository(session).get(_uuid(resource_id, "order_id"))
                return {str(row.account_id)} if row else set()
            if "thread_id" in payload:
                row = MessageRepository(session).get_thread(_uuid(resource_id, "thread_id"))
                return {str(row.account_id)} if row else set()
            task = ShippingTaskRepository(session).get(_uuid(resource_id, "task_id"))
            if task is None:
                return set()
            order = MarketplaceOrderRepository(session).get(task.order_id)
            return {str(order.account_id)} if order else set()

    # ---------------------------------------------------------------- audit
    def _audit_request(
        self, principal: McpServicePrincipal, tool_name: str, *, authorized: bool
    ) -> None:
        with self._session_factory() as session:
            AuditEventRepository(session).append(
                event_type="mcp.request" if authorized else "mcp.authorization_failed",
                actor_type="service_principal",
                actor_id=principal.name,
                resource_type="mcp_tool",
                resource_id=principal.id,
                details={"tool": tool_name, "authorized": authorized},
            )
            session.commit()

    def _actor(self, principal: McpServicePrincipal) -> str:
        return f"service_principal:{principal.name}"

    # ----------------------------------------------------------- tool table
    def _build_tools(self) -> dict[str, _Tool]:
        return {
            "inventory.search": _Tool("inventory:read", _t_inventory_search),
            "inventory.get": _Tool("inventory:read", _t_inventory_get),
            "inventory.create_draft": _Tool("inventory:write_draft", _t_inventory_create_draft),
            "inventory.propose_update": _Tool(
                "inventory:write_draft", _t_inventory_propose_update
            ),
            "inventory.list_images": _Tool("inventory:read", _t_inventory_list_images),
            "inventory.list_measurements": _Tool(
                "inventory:read", _t_inventory_list_measurements
            ),
            "research.create": _Tool("research:write", _t_research_create),
            "research.get": _Tool("research:read", _t_research_get),
            "research.add_source": _Tool("research:write", _t_research_add_source),
            "research.add_candidate": _Tool("research:write", _t_research_add_candidate),
            "research.complete": _Tool("research:write", _t_research_complete),
            "research.find_comparables": _Tool(
                "research:read", _t_research_find_comparables
            ),
            "pricing.calculate": _Tool("pricing:calculate", _t_pricing_calculate),
            "pricing.create_recommendation": _Tool(
                "pricing:calculate", _t_pricing_create_recommendation
            ),
            "listing.create_master_draft": _Tool(
                "listing:write_draft", _t_listing_create_master_draft
            ),
            "listing.create_variant": _Tool("listing:write_draft", _t_listing_create_variant),
            "listing.validate_draft": _Tool("listing:read", _t_listing_validate_draft),
            "listing.request_approval": _Tool(
                "approval:request", _t_listing_request_approval
            ),
            "approval.get": _Tool("approval:read", _t_approval_get),
            "approval.list": _Tool("approval:read", _t_approval_list),
            "approval.submit": _Tool("approval:request", _t_approval_submit),
            # Milestone six: genuine marketplace read/write tools (no cookies exposed).
            "marketplace.account_status": _Tool("marketplace:read", _t_mp_account_status),
            "marketplace.read_listings": _Tool("marketplace:read", _t_mp_read_listings),
            "marketplace.read_listing": _Tool("marketplace:read", _t_mp_read_listing),
            "marketplace.read_orders": _Tool("marketplace:order:read", _t_mp_read_orders),
            "marketplace.read_offers": _Tool("marketplace:offer:read", _t_mp_read_offers),
            "marketplace.read_messages": _Tool("marketplace:message:read", _t_mp_read_messages),
            "marketplace.read_notifications": _Tool(
                "marketplace:read", _t_mp_read_notifications
            ),
            "marketplace.sync_account": _Tool("marketplace:sync", _t_mp_sync_account),
            "marketplace.create_listing": _Tool(
                "marketplace:listing:create", _t_mp_create_listing
            ),
            "marketplace.refresh_listing": _Tool(
                "marketplace:listing:refresh", _t_mp_refresh_listing
            ),
            "marketplace.update_listing": _Tool(
                "marketplace:listing:update", _t_mp_update_listing
            ),
            "marketplace.promote_listing": _Tool(
                "marketplace:listing:promote", _t_mp_promote_listing
            ),
            "marketplace.share_listing": _Tool(
                "marketplace:listing:share", _t_mp_share_listing
            ),
            "marketplace.end_listing": _Tool("marketplace:listing:end", _t_mp_end_listing),
            "marketplace.send_offer": _Tool("marketplace:offer:respond", _t_mp_send_offer),
            "marketplace.accept_offer": _Tool("marketplace:offer:respond", _t_mp_accept_offer),
            "marketplace.decline_offer": _Tool("marketplace:offer:respond", _t_mp_decline_offer),
            "marketplace.counter_offer": _Tool("marketplace:offer:respond", _t_mp_counter_offer),
            "marketplace.send_message": _Tool(
                "marketplace:message:routine", _t_mp_send_message
            ),
            "marketplace.update_tracking": _Tool(
                "marketplace:tracking:update", _t_mp_update_tracking
            ),
            "marketplace.purchase_label": _Tool(
                "marketplace:label:purchase", _t_mp_purchase_label
            ),
        }


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #


def _inventory_dict(item) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "sku": item.sku,
        "title": item.title,
        "brand": item.brand,
        "model_name": item.model_name,
        "category": item.category,
        "status": item.status.value,
        "size_label": item.size_label,
        "condition": item.condition.value,
        "version": item.version,
    }


def _proposal_dict(proposal) -> dict[str, Any]:
    return {
        "id": str(proposal.id),
        "type": proposal.proposal_type.value,
        "resource_type": proposal.resource_type,
        "resource_id": str(proposal.resource_id),
        "current_version": proposal.current_version,
        "status": proposal.status.value,
        "risk_tier": proposal.risk_tier,
        "requested_by": proposal.requested_by,
    }


# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #


def _t_inventory_search(server: GoliathMcpServer, principal, payload):
    query = _require(payload, "query")
    limit = int(payload.get("limit", 25))
    items = server._service.search_inventory(str(query), limit=min(limit, 100))
    return {"items": [_inventory_dict(item) for item in items]}


def _t_inventory_get(server: GoliathMcpServer, principal, payload):
    item = server._service.get_inventory(_uuid(_require(payload, "item_id"), "item_id"))
    return _inventory_dict(item)


def _t_inventory_create_draft(server: GoliathMcpServer, principal, payload):
    fields = dict(payload)
    fields.setdefault("status", "draft")
    item = server._service.create_inventory(actor=server._actor(principal), **fields)
    return _inventory_dict(item)


def _t_inventory_propose_update(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    changes = _require(payload, "changes")
    if not isinstance(changes, dict):
        raise McpToolError("changes must be an object")
    item = server._service.get_inventory(item_id)
    proposal = server._service.submit_proposal(
        actor=server._actor(principal),
        proposal_type=ProposalType.INVENTORY_UPDATE,
        resource_type="inventory_item",
        resource_id=item_id,
        current_version=item.version,
        proposed_payload=changes,
        justification=str(payload.get("justification", "agent-proposed inventory update")),
        risk_tier=str(payload.get("risk_tier", "medium")),
    )
    return _proposal_dict(proposal)


def _t_inventory_list_images(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    media = server._service.list_media(item_id)
    return {
        "images": [
            {
                "id": str(m.id),
                "role": m.role.value,
                "media_type": m.media_type,
                "checksum": m.checksum,
            }
            for m in media
        ]
    }


def _t_inventory_list_measurements(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    values = server._service.list_measurements(item_id)
    return {
        "measurements": [
            {
                "type": m.measurement_type,
                "value": str(m.value),
                "unit": m.unit.value,
                "value_cm": str(m.value_cm),
            }
            for m in values
        ]
    }


def _t_research_create(server: GoliathMcpServer, principal, payload):
    record = server._service.create_research(
        _uuid(_require(payload, "item_id"), "item_id"),
        actor=server._actor(principal),
        research_question=str(_require(payload, "research_question")),
        researcher=str(payload.get("researcher", principal.name)),
        search_terms=payload.get("search_terms"),
    )
    return {"id": str(record.id), "status": record.status.value}


def _t_research_get(server: GoliathMcpServer, principal, payload):
    record = server._service.get_research(
        _uuid(_require(payload, "research_id"), "research_id")
    )
    return {
        "id": str(record.id),
        "status": record.status.value,
        "confidence": str(record.confidence) if record.confidence is not None else None,
        "selected_identification": record.selected_identification,
    }


def _t_research_add_source(server: GoliathMcpServer, principal, payload):
    source = server._service.add_research_source(
        _uuid(_require(payload, "research_id"), "research_id"),
        actor=server._actor(principal),
        source_type=str(_require(payload, "source_type")),
        content_hash=str(_require(payload, "content_hash")),
        title=payload.get("title"),
        url=payload.get("url"),
        excerpt=payload.get("excerpt"),
        relevance_score=(
            _decimal(payload["relevance_score"], "relevance_score")
            if payload.get("relevance_score") is not None
            else None
        ),
        reliability=SourceReliability(payload.get("reliability", "unknown")),
    )
    return {"id": str(source.id)}


def _t_research_add_candidate(server: GoliathMcpServer, principal, payload):
    candidate = server._service.add_research_candidate(
        _uuid(_require(payload, "research_id"), "research_id"),
        actor=server._actor(principal),
        brand=payload.get("brand"),
        model_name=payload.get("model_name"),
        attributes=payload.get("attributes"),
        confidence=(
            _decimal(payload["confidence"], "confidence")
            if payload.get("confidence") is not None
            else None
        ),
        evidence_source_ids=payload.get("evidence_source_ids"),
        rationale=payload.get("rationale"),
    )
    return {"id": str(candidate.id)}


def _t_research_complete(server: GoliathMcpServer, principal, payload):
    record, proposal_id = server._service.complete_research(
        _uuid(_require(payload, "research_id"), "research_id"),
        actor=server._actor(principal),
        status=ResearchStatus(str(_require(payload, "status"))),
        confidence=(
            _decimal(payload["confidence"], "confidence")
            if payload.get("confidence") is not None
            else None
        ),
        selected_identification=payload.get("selected_identification"),
        observations=payload.get("observations"),
        unresolved_questions=payload.get("unresolved_questions"),
    )
    return {
        "id": str(record.id),
        "status": record.status.value,
        "proposal_id": str(proposal_id) if proposal_id else None,
    }


def _t_research_find_comparables(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    comparables = server._service.find_comparables(item_id)
    return {
        "comparables": [
            {
                "id": str(c.id),
                "marketplace": c.marketplace.value,
                "is_sold": c.is_sold,
                "sold_price": str(c.sold_price) if c.sold_price is not None else None,
                "listed_price": str(c.listed_price) if c.listed_price is not None else None,
            }
            for c in comparables
        ]
    }


def _pricing_inputs(payload: dict[str, Any]) -> PricingInputs:
    observations = [
        ComparableObservation(
            price=_decimal(entry["price"], "comparable.price"),
            is_sold=bool(entry.get("is_sold", False)),
            reliability=float(entry.get("reliability", 0.5)),
        )
        for entry in payload.get("comparables", [])
    ]
    return PricingInputs(
        cost_basis=_decimal(_require(payload, "cost_basis"), "cost_basis"),
        currency=str(payload.get("currency", "USD")),
        shipping_cost=_decimal(payload.get("shipping_cost", 0), "shipping_cost"),
        promotion_cost=_decimal(payload.get("promotion_cost", 0), "promotion_cost"),
        expected_offer_discount=float(payload.get("expected_offer_discount", 0.0)),
        minimum_acceptable_profit=(
            _decimal(payload["minimum_acceptable_profit"], "minimum_acceptable_profit")
            if payload.get("minimum_acceptable_profit") is not None
            else None
        ),
        condition=str(payload.get("condition", "good")),
        is_stale=bool(payload.get("is_stale", False)),
        comparables=observations,
    )


def _pricing_result_dict(result) -> dict[str, Any]:
    return {
        "recommended_price": str(result.recommended_price),
        "fast_sale_price": str(result.fast_sale_price),
        "minimum_price": str(result.minimum_price),
        "expected_net_proceeds": str(result.expected_net_proceeds),
        "expected_profit": str(result.expected_profit),
        "expected_margin": str(result.expected_margin),
        "confidence": str(result.confidence),
        "fee_version": result.fee_version,
        "breakdown": result.breakdown,
        "warnings": result.warnings,
    }


def _t_pricing_calculate(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    result = server._service.calculate_pricing(
        item_id,
        actor=server._actor(principal),
        inputs=_pricing_inputs(payload),
        fee_version=payload.get("fee_version"),
    )
    return _pricing_result_dict(result)


def _t_pricing_create_recommendation(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    recommendation, result = server._service.create_pricing_recommendation(
        item_id,
        actor=server._actor(principal),
        inputs=_pricing_inputs(payload),
        fee_version=payload.get("fee_version"),
    )
    data = _pricing_result_dict(result)
    data["recommendation_id"] = str(recommendation.id)
    return data


def _t_listing_create_master_draft(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    draft = server._service.create_master_draft(item_id, actor=server._actor(principal))
    return {
        "id": str(draft.id),
        "status": draft.status.value,
        "validation_warnings": draft.validation_warnings,
        "missing_fields": draft.missing_fields,
    }


def _t_listing_create_variant(server: GoliathMcpServer, principal, payload):
    variant = server._service.create_variant(
        _uuid(_require(payload, "draft_id"), "draft_id"),
        actor=server._actor(principal),
        marketplace=Marketplace(str(_require(payload, "marketplace"))),
        proposed_price=(
            _decimal(payload["proposed_price"], "proposed_price")
            if payload.get("proposed_price") is not None
            else None
        ),
    )
    return {
        "id": str(variant.id),
        "marketplace": variant.marketplace.value,
        "marketplace_title": variant.marketplace_title,
        "validation_warnings": variant.validation_warnings,
    }


def _t_listing_validate_draft(server: GoliathMcpServer, principal, payload):
    draft_id = _uuid(_require(payload, "draft_id"), "draft_id")
    return server._service.validate_draft(draft_id, actor=server._actor(principal))


def _t_listing_request_approval(server: GoliathMcpServer, principal, payload):
    proposal = server._service.request_listing_approval(
        _uuid(_require(payload, "draft_id"), "draft_id"), actor=server._actor(principal)
    )
    return _proposal_dict(proposal)


def _t_approval_get(server: GoliathMcpServer, principal, payload):
    proposal = server._service.get_proposal(
        _uuid(_require(payload, "proposal_id"), "proposal_id")
    )
    return _proposal_dict(proposal)


def _t_approval_list(server: GoliathMcpServer, principal, payload):
    status = payload.get("status")
    proposals = server._service.list_proposals(
        status=ProposalStatus(status) if status else None,
        limit=min(int(payload.get("limit", 50)), 200),
    )
    return {"proposals": [_proposal_dict(p) for p in proposals]}


def _t_approval_submit(server: GoliathMcpServer, principal, payload):
    proposal = server._service.submit_proposal(
        actor=server._actor(principal),
        proposal_type=ProposalType(str(_require(payload, "proposal_type"))),
        resource_type=str(_require(payload, "resource_type")),
        resource_id=_uuid(_require(payload, "resource_id"), "resource_id"),
        current_version=int(_require(payload, "current_version")),
        proposed_payload=dict(_require(payload, "proposed_payload")),
        justification=str(_require(payload, "justification")),
        risk_tier=str(payload.get("risk_tier", "medium")),
    )
    return _proposal_dict(proposal)


# --------------------------------------------------------------------------- #
# Milestone six marketplace tool handlers (bounded; never expose session state)
# --------------------------------------------------------------------------- #


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


def _t_mp_account_status(server: GoliathMcpServer, principal, payload):
    account = server._marketplace.get_account(_uuid(_require(payload, "account_id"), "account_id"))
    return {
        "id": str(account.id),
        "marketplace": account.marketplace.value,
        "mode": account.automation_mode.value,
        "status": account.status.value,
    }


def _t_mp_read_listings(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.read_listings_remote(
            _uuid(_require(payload, "account_id"), "account_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_read_listing(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.read_listing(
            _uuid(_require(payload, "listing_id"), "listing_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_read_orders(server: GoliathMcpServer, principal, payload):
    account_id = _uuid(_require(payload, "account_id"), "account_id")
    receipt = _run_async(
        server._marketplace.read_orders_remote(
            account_id,
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_read_offers(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.read_offers_remote(
            _uuid(_require(payload, "account_id"), "account_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_read_messages(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.read_messages_remote(
            _uuid(_require(payload, "account_id"), "account_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_read_notifications(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.read_notifications_remote(
            _uuid(_require(payload, "account_id"), "account_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_sync_account(server: GoliathMcpServer, principal, payload):
    account_id = _uuid(_require(payload, "account_id"), "account_id")
    result = _run_async(
        server._marketplace.sync_account(
            account_id, principal_scopes=set(principal.scopes), actor=server._actor(principal)
        )
    )
    if isinstance(result, dict) and isinstance(result.get("adapter"), dict):
        result = dict(result)
        result["adapter"] = _safe_marketplace_payload("sync_account", result["adapter"])
    return result


def _t_mp_create_listing(server: GoliathMcpServer, principal, payload):
    item_id = _uuid(_require(payload, "item_id"), "item_id")
    account_ids = [_uuid(a, "account_id") for a in payload.get("account_ids", [])] or None
    return _run_async(
        server._marketplace.publish_item(
            item_id,
            account_ids=account_ids,
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )


def _t_mp_refresh_listing(server: GoliathMcpServer, principal, payload):
    listing_id = _uuid(_require(payload, "listing_id"), "listing_id")
    receipt = _run_async(
        server._marketplace.refresh_listing(
            listing_id, principal_scopes=set(principal.scopes), actor=server._actor(principal)
        )
    )
    return {"ok": receipt.ok, "verified": receipt.verified}


def _t_mp_update_listing(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.update_listing(
            _uuid(_require(payload, "listing_id"), "listing_id"),
            fields=dict(_require(payload, "fields")),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_promote_listing(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.promote_listing(
            _uuid(_require(payload, "listing_id"), "listing_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_share_listing(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.share_listing(
            _uuid(_require(payload, "listing_id"), "listing_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_end_listing(server: GoliathMcpServer, principal, payload):
    listing_id = _uuid(_require(payload, "listing_id"), "listing_id")
    receipt = _run_async(
        server._marketplace.end_listing(
            listing_id, principal_scopes=set(principal.scopes), actor=server._actor(principal)
        )
    )
    return {"ok": receipt.ok, "verified": receipt.verified}


def _execute_policy_offer(server: GoliathMcpServer, principal, payload, requested: str):
    offer_id = _uuid(_require(payload, "offer_id"), "offer_id")
    counter_amount = payload.get("counter_amount")
    if requested == "counter" and counter_amount is None:
        raise McpToolError("counter_offer requires counter_amount")
    result = _run_async(
        server._marketplace.handle_offer(
            offer_id,
            requested_action=requested,
            requested_counter_amount=(
                _decimal(counter_amount, "counter_amount") if counter_amount is not None else None
            ),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return result


def _t_mp_accept_offer(server: GoliathMcpServer, principal, payload):
    return _execute_policy_offer(server, principal, payload, "accept")


def _t_mp_decline_offer(server: GoliathMcpServer, principal, payload):
    return _execute_policy_offer(server, principal, payload, "decline")


def _t_mp_counter_offer(server: GoliathMcpServer, principal, payload):
    return _execute_policy_offer(server, principal, payload, "counter")


def _t_mp_send_offer(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.send_offer(
            _uuid(_require(payload, "listing_id"), "listing_id"),
            _decimal(_require(payload, "amount"), "amount"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_send_message(server: GoliathMcpServer, principal, payload):
    thread_id = _uuid(_require(payload, "thread_id"), "thread_id")
    return _run_async(
        server._marketplace.respond_message(
            thread_id,
            str(_require(payload, "body")),
            facts=payload.get("facts", {}),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )


def _t_mp_update_tracking(server: GoliathMcpServer, principal, payload):
    receipt = _run_async(
        server._marketplace.update_tracking(
            _uuid(_require(payload, "order_id"), "order_id"),
            str(_require(payload, "tracking_number")),
            str(_require(payload, "carrier")),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )
    return _receipt_dict(receipt)


def _t_mp_purchase_label(server: GoliathMcpServer, principal, payload):
    return _run_async(
        server._marketplace.purchase_label(
            _uuid(_require(payload, "task_id"), "task_id"),
            principal_scopes=set(principal.scopes),
            actor=server._actor(principal),
        )
    )


def _receipt_dict(receipt) -> dict[str, Any]:
    return {
        "ok": receipt.ok,
        "operation": receipt.operation,
        "executed": receipt.executed,
        "shadowed": receipt.shadowed,
        "verified": receipt.verified,
        "remote_id": receipt.remote_id,
        "error_category": receipt.error_category,
        "reasons": receipt.reasons,
        "data": _safe_marketplace_payload(receipt.operation, receipt.data),
    }


def _safe_marketplace_payload(operation: str, payload: Any) -> dict[str, Any]:
    """Return a narrow, buyer-safe projection of adapter output.

    Adapter payloads are internal and may contain arbitrary marketplace fields.
    MCP callers receive only stable identifiers and operational state.
    """
    if not isinstance(payload, dict):
        return {}
    keys = {
        "read_listings": {"listings"},
        "read_listing": {"id", "remote_listing_id", "status", "title", "price", "currency"},
        "read_orders": {"orders"},
        "read_order": {"id", "remote_order_id", "status", "sale_price", "currency", "tracking_number", "carrier"},
        "read_offers": {"offers"},
        "read_messages": {"messages"},
        "read_notifications": {"notifications"},
        "purchase_label": {"label_reference", "tracking_number", "carrier", "cost"},
        "update_tracking": {"tracking_number", "carrier"},
    }.get(operation)
    if keys is None:
        # Write receipts have operation-specific, non-sensitive confirmations.
        keys = {
            "status", "refreshed", "promoted", "shared", "state", "sent", "delivered",
            "label_reference", "tracking_number", "carrier", "cost", "amount", "category",
        }
    result = {key: payload[key] for key in keys if key in payload}
    if operation in {"read_listings", "read_orders", "read_offers", "read_messages", "read_notifications"}:
        collection = next(iter(keys))
        values = payload.get(collection, [])
        if not isinstance(values, list):
            result[collection] = []
        else:
            result[collection] = [
                _safe_marketplace_row(operation, value) for value in values if isinstance(value, dict)
            ]
    return result


def _safe_marketplace_row(operation: str, row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "read_listings": {"id", "remote_listing_id", "status", "title", "price", "currency"},
        "read_orders": {"id", "remote_order_id", "status", "sale_price", "currency", "tracking_number", "carrier"},
        "read_offers": {"id", "remote_offer_id", "status", "offer_amount", "list_price", "currency"},
        "read_messages": {"id", "remote_thread_id", "category", "direction", "delivered"},
        "read_notifications": {"id", "type", "status", "created_at"},
    }.get(operation, set())
    return {key: row[key] for key in allowed if key in row}
