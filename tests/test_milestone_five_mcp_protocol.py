"""Milestone five: standards-compliant MCP protocol integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.domain_repositories import McpPrincipalRepository
from goliath.mcp.protocol import (
    METHOD_NOT_FOUND,
    SERVER_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    GoliathMcpProtocol,
)
from goliath.mcp.server import FORBIDDEN_MCP_TOOLS, GoliathMcpServer

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


@pytest.fixture
def protocol(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media.media_root = tmp_path / "m"
    config.domain.media.quarantine_root = tmp_path / "q"
    config.domain.media.temp_upload_root = tmp_path / "t"
    server = GoliathMcpServer(session_factory=factory, config=config)
    with factory() as session:
        _, cred = McpPrincipalRepository(session).create(name="hermes", scopes=ALL_SCOPES)
        _, read_only = McpPrincipalRepository(session).create(
            name="ro", scopes=["inventory:read"]
        )
        session.commit()
    yield GoliathMcpProtocol(server), cred, read_only
    engine.dispose()


def _rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_initialize_negotiates_protocol_version(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(_rpc("initialize", {"protocolVersion": "2025-06-18"}), credential=cred)
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "goliath-mcp"


def test_initialize_falls_back_for_unknown_version(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(_rpc("initialize", {"protocolVersion": "1999-01-01"}), credential=cred)
    assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_notifications_return_no_response(protocol) -> None:
    proto, cred, _ = protocol
    assert proto.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, credential=cred) is None


def test_tool_discovery_lists_all_tools_with_schemas(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(_rpc("tools/list"), credential=cred)
    tools = resp["result"]["tools"]
    assert len(tools) >= 21
    assert all("inputSchema" in tool for tool in tools)
    names = {tool["name"] for tool in tools}
    assert not (names & FORBIDDEN_MCP_TOOLS)


def test_tools_call_success_and_structured_content(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(
        _rpc(
            "tools/call",
            {
                "name": "inventory.create_draft",
                "arguments": {
                    "sku": "P1",
                    "title": "Tee",
                    "condition": "good",
                    "acquisition_cost": "10",
                },
            },
        ),
        credential=cred,
    )
    assert resp["result"]["isError"] is False
    assert "id" in resp["result"]["structuredContent"]


def test_tools_call_domain_error_is_result_not_protocol_error(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(
        _rpc("tools/call", {"name": "inventory.get", "arguments": {"item_id": "not-a-uuid"}}),
        credential=cred,
    )
    assert resp["result"]["isError"] is True


def test_authentication_failure_is_protocol_error(protocol) -> None:
    proto, _, _ = protocol
    resp = proto.handle(
        _rpc("tools/call", {"name": "inventory.search", "arguments": {"query": "x"}}),
        credential="bogus",
    )
    assert resp["error"]["code"] == SERVER_ERROR


def test_scope_enforcement_on_tool_call(protocol) -> None:
    proto, _, read_only = protocol
    resp = proto.handle(
        _rpc(
            "tools/call",
            {"name": "inventory.create_draft", "arguments": {"sku": "x", "title": "t", "acquisition_cost": "1"}},
        ),
        credential=read_only,
    )
    assert resp["error"]["code"] == SERVER_ERROR
    assert "scope" in resp["error"]["message"]


def test_resources_list_and_read(protocol) -> None:
    proto, cred, _ = protocol
    created = proto.handle(
        _rpc(
            "tools/call",
            {
                "name": "inventory.create_draft",
                "arguments": {"sku": "R1", "title": "Tee", "condition": "good", "acquisition_cost": "5"},
            },
        ),
        credential=cred,
    )
    item_id = created["result"]["structuredContent"]["id"]
    listing = proto.handle(_rpc("resources/list"), credential=cred)
    assert len(listing["result"]["resourceTemplates"]) == 6
    read = proto.handle(
        _rpc("resources/read", {"uri": f"goliath://inventory/{item_id}/summary"}), credential=cred
    )
    assert "contents" in read["result"]


def test_resource_scope_enforced(protocol) -> None:
    proto, _, read_only = protocol
    resp = proto.handle(
        _rpc("resources/read", {"uri": "goliath://approvals/queue"}), credential=read_only
    )
    assert resp["error"]["code"] == SERVER_ERROR


def test_prompts_list_and_get(protocol) -> None:
    proto, cred, _ = protocol
    listing = proto.handle(_rpc("prompts/list"), credential=cred)
    assert len(listing["result"]["prompts"]) == 5
    got = proto.handle(
        _rpc("prompts/get", {"name": "product_identification", "arguments": {"item_id": "abc"}}),
        credential=cred,
    )
    assert got["result"]["messages"][0]["role"] == "user"


def test_prompt_injection_is_escaped(protocol) -> None:
    proto, cred, _ = protocol
    got = proto.handle(
        _rpc("prompts/get", {"name": "product_identification", "arguments": {"item_id": "{evil}"}}),
        credential=cred,
    )
    text = got["result"]["messages"][0]["content"]["text"]
    assert "{evil}" not in text


def test_unknown_method_returns_method_not_found(protocol) -> None:
    proto, cred, _ = protocol
    resp = proto.handle(_rpc("does/not/exist"), credential=cred)
    assert resp["error"]["code"] == METHOD_NOT_FOUND
    assert proto.protocol_error_count == 1


def test_cancellation_notification_accepted(protocol) -> None:
    proto, cred, _ = protocol
    result = proto.handle(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}},
        credential=cred,
    )
    assert result is None


def test_ping_and_shutdown(protocol) -> None:
    proto, cred, _ = protocol
    assert proto.handle(_rpc("ping"), credential=cred)["result"] == {}
    proto.handle(_rpc("shutdown"), credential=cred)
    assert proto.is_shutting_down
