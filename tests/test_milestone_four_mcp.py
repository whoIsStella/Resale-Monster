"""Milestone four: MCP server authentication, scopes, tools, and safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from goliath.agents.domain_routing import (
    InsufficientMcpScopesError,
    missing_mcp_scopes,
    required_mcp_scopes,
    select_domain_agent,
)
from goliath.config import AgentConfig, OrchestrationConfig
from goliath.core.schemas import TaskType
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.domain_repositories import McpPrincipalRepository
from goliath.mcp.server import (
    FORBIDDEN_MCP_TOOLS,
    GoliathMcpServer,
    McpAuthenticationError,
    McpAuthorizationError,
    McpToolError,
    McpUnknownToolError,
)

ALL_SCOPES = [
    "inventory:read",
    "inventory:write_draft",
    "research:read",
    "research:write",
    "pricing:calculate",
    "listing:read",
    "listing:write_draft",
    "approval:read",
    "approval:request",
]

EXPECTED_TOOLS = {
    "inventory.search",
    "inventory.get",
    "inventory.create_draft",
    "inventory.propose_update",
    "inventory.list_images",
    "inventory.list_measurements",
    "research.create",
    "research.get",
    "research.add_source",
    "research.add_candidate",
    "research.complete",
    "research.find_comparables",
    "pricing.calculate",
    "pricing.create_recommendation",
    "listing.create_master_draft",
    "listing.create_variant",
    "listing.validate_draft",
    "listing.request_approval",
    "approval.get",
    "approval.list",
    "approval.submit",
}


@pytest.fixture
def server(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    media_root = tmp_path / "media"
    media_root.mkdir()
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media_storage_root = media_root
    factory = build_session_factory(engine)
    srv = GoliathMcpServer(session_factory=factory, config=config)
    yield srv, factory
    engine.dispose()


def _principal(factory, scopes):
    with factory() as session:
        _, raw = McpPrincipalRepository(session).create(name="hermes", scopes=scopes)
        session.commit()
    return raw


def test_all_initial_tools_present(server) -> None:
    srv, _ = server
    # The milestone-four resale tools remain present (later milestones add more).
    assert EXPECTED_TOOLS <= set(srv.tool_names())


def test_forbidden_tools_are_absent(server) -> None:
    srv, _ = server
    assert not (set(srv.tool_names()) & FORBIDDEN_MCP_TOOLS)
    for forbidden in FORBIDDEN_MCP_TOOLS:
        with pytest.raises(McpUnknownToolError):
            srv.call(forbidden, credential="anything", arguments={})


def test_authentication_required(server) -> None:
    srv, _ = server
    with pytest.raises(McpAuthenticationError):
        srv.call("inventory.search", credential="bogus", arguments={"query": "x"})


def test_scope_enforced(server) -> None:
    srv, factory = server
    read_only = _principal(factory, ["inventory:read"])
    with pytest.raises(McpAuthorizationError):
        srv.call(
            "inventory.create_draft",
            credential=read_only,
            arguments={"sku": "X", "title": "t", "condition": "good", "acquisition_cost": "1"},
        )


def test_every_tool_executes_end_to_end(server) -> None:
    srv, factory = server
    cred = _principal(factory, ALL_SCOPES)

    created = srv.call(
        "inventory.create_draft",
        credential=cred,
        arguments={
            "sku": "MCP-1",
            "title": "Nike Tee",
            "condition": "good",
            "acquisition_cost": "10",
            "category": "clothing",
            "size_label": "M",
            "materials": ["cotton"],
        },
    )
    item_id = created["id"]

    assert srv.call("inventory.get", credential=cred, arguments={"item_id": item_id})["sku"] == "MCP-1"
    assert srv.call("inventory.search", credential=cred, arguments={"query": "nike"})["items"]
    assert "images" in srv.call("inventory.list_images", credential=cred, arguments={"item_id": item_id})
    assert "measurements" in srv.call(
        "inventory.list_measurements", credential=cred, arguments={"item_id": item_id}
    )

    proposal = srv.call(
        "inventory.propose_update",
        credential=cred,
        arguments={"item_id": item_id, "changes": {"brand": "Nike"}, "justification": "id"},
    )
    assert proposal["type"] == "inventory_update"

    research = srv.call(
        "research.create",
        credential=cred,
        arguments={"item_id": item_id, "research_question": "model?"},
    )
    rid = research["id"]
    assert srv.call("research.get", credential=cred, arguments={"research_id": rid})["status"]
    srv.call(
        "research.add_source",
        credential=cred,
        arguments={"research_id": rid, "source_type": "web", "content_hash": "h1", "reliability": "high"},
    )
    srv.call(
        "research.add_candidate",
        credential=cred,
        arguments={"research_id": rid, "brand": "Nike", "confidence": "0.9"},
    )
    completed = srv.call(
        "research.complete",
        credential=cred,
        arguments={
            "research_id": rid,
            "status": "completed",
            "confidence": "0.9",
            "selected_identification": {"brand": "Nike"},
        },
    )
    assert completed["proposal_id"]
    assert "comparables" in srv.call(
        "research.find_comparables", credential=cred, arguments={"item_id": item_id}
    )

    pricing = srv.call(
        "pricing.calculate",
        credential=cred,
        arguments={
            "item_id": item_id,
            "cost_basis": "10",
            "comparables": [{"price": "40", "is_sold": True}],
        },
    )
    assert "recommended_price" in pricing
    rec = srv.call(
        "pricing.create_recommendation",
        credential=cred,
        arguments={"item_id": item_id, "cost_basis": "10", "comparables": [{"price": "40", "is_sold": True}]},
    )
    assert rec["recommendation_id"]

    draft = srv.call(
        "listing.create_master_draft", credential=cred, arguments={"item_id": item_id}
    )
    draft_id = draft["id"]
    assert draft["status"] == "draft"
    variant = srv.call(
        "listing.create_variant",
        credential=cred,
        arguments={"draft_id": draft_id, "marketplace": "ebay"},
    )
    assert variant["marketplace"] == "ebay"
    assert "warnings" in srv.call(
        "listing.validate_draft", credential=cred, arguments={"draft_id": draft_id}
    )
    approval = srv.call(
        "listing.request_approval", credential=cred, arguments={"draft_id": draft_id}
    )
    approval_id = approval["id"]
    assert srv.call("approval.get", credential=cred, arguments={"proposal_id": approval_id})["id"]
    assert srv.call("approval.list", credential=cred, arguments={})["proposals"]

    submitted = srv.call(
        "approval.submit",
        credential=cred,
        arguments={
            "proposal_type": "inventory_update",
            "resource_type": "inventory_item",
            "resource_id": item_id,
            "current_version": 1,
            "proposed_payload": {"pattern": "solid"},
            "justification": "cleanup",
        },
    )
    assert submitted["status"] == "pending"

    # Metrics captured per tool and no forbidden tool executed.
    assert srv.metrics.requests_by_tool["inventory.get"] >= 1
    assert srv.metrics.authorization_failures == 0


def test_tool_returns_controlled_error(server) -> None:
    srv, factory = server
    cred = _principal(factory, ALL_SCOPES)
    with pytest.raises(McpToolError):
        srv.call("inventory.get", credential=cred, arguments={"item_id": "not-a-uuid"})


# --------------------------------------------------------------- domain routing


def test_domain_task_requires_mcp_scopes() -> None:
    hermes = AgentConfig(
        command_template=["hermes"],
        mcp_scopes={"inventory:read", "research:read", "research:write", "approval:request"},
    )
    codex = AgentConfig(command_template=["codex"], mcp_scopes=set())
    agents = {"hermes": hermes, "codex": codex}
    assert missing_mcp_scopes(codex, TaskType.PRODUCT_IDENTIFICATION)
    assert not missing_mcp_scopes(hermes, TaskType.PRODUCT_IDENTIFICATION)
    assert select_domain_agent(TaskType.PRODUCT_IDENTIFICATION, agents) == "hermes"


def test_domain_routing_rejects_agent_without_scopes() -> None:
    codex = AgentConfig(command_template=["codex"], mcp_scopes=set())
    with pytest.raises(InsufficientMcpScopesError):
        select_domain_agent(TaskType.PRICING_ANALYSIS, {"codex": codex})


def test_required_scopes_are_defined_for_all_domain_tasks() -> None:
    for task in (
        TaskType.PRODUCT_IDENTIFICATION,
        TaskType.COMPARABLE_RESEARCH,
        TaskType.LISTING_GENERATION,
        TaskType.PRICING_ANALYSIS,
    ):
        assert required_mcp_scopes(task)
