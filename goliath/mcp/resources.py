"""Read-only MCP resources exposing bounded, non-sensitive domain summaries.

Resources enforce scopes, use stable URIs, return typed JSON content, and append
audit events. They never expose credentials, tokens, buyer data, or file bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from goliath.db.models import DraftStatus, ProposalStatus
from goliath.db.repositories import AuditEventRepository


@dataclass(slots=True)
class ResourceTemplate:
    uri_template: str
    name: str
    description: str
    scope: str


RESOURCE_TEMPLATES: tuple[ResourceTemplate, ...] = (
    ResourceTemplate(
        "goliath://inventory/{item_id}/summary",
        "inventory-summary",
        "Non-sensitive inventory item summary",
        "inventory:read",
    ),
    ResourceTemplate(
        "goliath://research/{research_id}",
        "research-record",
        "Research record status and identification",
        "research:read",
    ),
    ResourceTemplate(
        "goliath://listing-drafts/approved",
        "approved-listing-drafts",
        "Approved master listing drafts",
        "listing:read",
    ),
    ResourceTemplate(
        "goliath://pricing/{item_id}/recommendations",
        "pricing-recommendations",
        "Pricing recommendations for an item",
        "pricing:calculate",
    ),
    ResourceTemplate(
        "goliath://completeness/{item_id}",
        "completeness-report",
        "Deterministic completeness report",
        "inventory:read",
    ),
    ResourceTemplate(
        "goliath://approvals/queue",
        "approval-queue",
        "Pending approval queue summary",
        "approval:read",
    ),
)


class ResourceError(ValueError):
    pass


class ResourceScopeError(PermissionError):
    pass


def list_resources() -> list[dict[str, Any]]:
    return [
        {
            "uriTemplate": template.uri_template,
            "name": template.name,
            "description": template.description,
            "mimeType": "application/json",
        }
        for template in RESOURCE_TEMPLATES
    ]


def _match(uri: str) -> tuple[ResourceTemplate, dict[str, str]]:
    for template in RESOURCE_TEMPLATES:
        parts = template.uri_template.split("/")
        given = uri.split("/")
        if len(parts) != len(given):
            continue
        params: dict[str, str] = {}
        ok = True
        for expected, actual in zip(parts, given, strict=True):
            if expected.startswith("{") and expected.endswith("}"):
                params[expected[1:-1]] = actual
            elif expected != actual:
                ok = False
                break
        if ok:
            return template, params
    raise ResourceError(f"unknown resource uri: {uri}")


def read_resource(
    uri: str,
    *,
    scopes: set[str],
    service,
    session_factory,
    principal_name: str,
    principal_id: UUID,
) -> dict[str, Any]:
    template, params = _match(uri)
    if template.scope not in scopes:
        raise ResourceScopeError(f"scope required: {template.scope}")

    content = _render(template, params, service)

    with session_factory() as session:
        AuditEventRepository(session).append(
            event_type="mcp.resource_access",
            actor_type="service_principal",
            actor_id=principal_name,
            resource_type="mcp_resource",
            resource_id=principal_id,
            details={"uri_template": template.uri_template},
        )
        session.commit()

    return {
        "contents": [
            {"uri": uri, "mimeType": "application/json", "text": json.dumps(content)}
        ]
    }


def _render(template: ResourceTemplate, params: dict[str, str], service) -> dict[str, Any]:
    name = template.name
    if name == "inventory-summary":
        item = service.get_inventory(UUID(params["item_id"]))
        return {
            "id": str(item.id),
            "sku": item.sku,
            "title": item.title,
            "brand": item.brand,
            "category": item.category,
            "status": item.status.value,
            "version": item.version,
        }
    if name == "research-record":
        record = service.get_research(UUID(params["research_id"]))
        return {
            "id": str(record.id),
            "status": record.status.value,
            "confidence": str(record.confidence) if record.confidence is not None else None,
        }
    if name == "approved-listing-drafts":
        drafts = service.list_drafts(status=DraftStatus.APPROVED, limit=50)
        return {"drafts": [{"id": str(d.id), "title": d.title} for d in drafts]}
    if name == "pricing-recommendations":
        from goliath.db.domain_repositories import PricingRecommendationRepository

        with service._session_factory() as session:
            recs = PricingRecommendationRepository(session).list_for_item(
                UUID(params["item_id"])
            )
            return {
                "recommendations": [
                    {
                        "id": str(r.id),
                        "recommended_price": str(r.recommended_price),
                        "policy_version": r.policy_version,
                    }
                    for r in recs
                ]
            }
    if name == "completeness-report":
        result = service.evaluate_completeness(UUID(params["item_id"]))
        return {
            "score": result.score,
            "blocking_errors": result.blocking_errors,
            "missing_required_fields": result.missing_required_fields,
        }
    if name == "approval-queue":
        proposals = service.list_proposals(status=ProposalStatus.PENDING, limit=50)
        return {
            "pending": [
                {"id": str(p.id), "type": p.proposal_type.value, "risk_tier": p.risk_tier}
                for p in proposals
            ]
        }
    raise ResourceError(f"no renderer for resource: {name}")
