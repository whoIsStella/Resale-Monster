"""Goliath MCP server: bounded resale-domain tools for agents.

The server exposes only safe, typed tools. It never exposes raw database
sessions, unrestricted SQL, shell execution, credentials, marketplace tokens,
or live-mutation actions. Every call authenticates a service principal, enforces
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
        if tool.scope not in set(principal.scopes):
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
            ValueError,
        ) as error:
            raise McpToolError(str(error)) from error

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
