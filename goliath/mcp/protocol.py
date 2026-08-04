"""Standards-compliant MCP JSON-RPC 2.0 protocol layer.

Implements the MCP methods (initialize/negotiation, tools, resources, prompts,
ping, cancellation) over an abstract message interface, so it can run over stdio
or authenticated HTTP without any network dependency in tests. It wraps the
bounded ``GoliathMcpServer`` and preserves service-principal authentication,
scope enforcement, audit events, resource-version checks, and forbidden-tool
absence.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from goliath.mcp import prompts as prompt_registry
from goliath.mcp import resources as resource_registry
from goliath.mcp.server import (
    GoliathMcpServer,
    McpAuthenticationError,
    McpAuthorizationError,
    McpToolError,
    McpUnknownToolError,
)

LOGGER = logging.getLogger("goliath.mcp")

# MCP protocol revisions this server understands (newest first).
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2024-11-05")
SERVER_INFO = {"name": "goliath-mcp", "version": "0.5.0"}

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
SERVER_ERROR = -32000

# Minimal typed input schemas for tools (JSON Schema).
_TOOL_REQUIRED: dict[str, list[str]] = {
    "inventory.get": ["item_id"],
    "inventory.create_draft": ["sku", "title", "acquisition_cost"],
    "inventory.propose_update": ["item_id", "changes"],
    "inventory.list_images": ["item_id"],
    "inventory.list_measurements": ["item_id"],
    "inventory.search": ["query"],
    "research.create": ["item_id", "research_question"],
    "research.get": ["research_id"],
    "research.add_source": ["research_id", "source_type", "content_hash"],
    "research.add_candidate": ["research_id"],
    "research.complete": ["research_id", "status"],
    "research.find_comparables": ["item_id"],
    "pricing.calculate": ["item_id", "cost_basis"],
    "pricing.create_recommendation": ["item_id", "cost_basis"],
    "listing.create_master_draft": ["item_id"],
    "listing.create_variant": ["draft_id", "marketplace"],
    "listing.validate_draft": ["draft_id"],
    "listing.request_approval": ["draft_id"],
    "approval.get": ["proposal_id"],
    "approval.list": [],
    "approval.submit": ["proposal_type", "resource_type", "resource_id", "current_version"],
}


class GoliathMcpProtocol:
    """Dispatches MCP JSON-RPC messages against the bounded Goliath tool server."""

    def __init__(self, server: GoliathMcpServer, *, logger: logging.Logger | None = None) -> None:
        self._server = server
        self._log = logger or LOGGER
        self._initialized = False
        self._shutdown = False
        self._cancelled_requests: set[Any] = set()
        self.protocol_error_count = 0

    # ------------------------------------------------------------- capabilities
    @staticmethod
    def server_capabilities() -> dict[str, Any]:
        return {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
            "logging": {},
        }

    def request_shutdown(self) -> None:
        """Graceful shutdown signal for the transport loop."""
        self._shutdown = True

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown

    # ------------------------------------------------------------------ dispatch
    def handle(self, message: dict[str, Any], *, credential: str | None = None) -> dict[str, Any] | None:
        """Handle one JSON-RPC message. Returns a response, or None for notifications."""
        if message.get("jsonrpc") != "2.0":
            return self._error(message.get("id"), INVALID_REQUEST, "invalid jsonrpc version")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method is None:
            return self._error(request_id, INVALID_REQUEST, "missing method")

        # Notifications carry no id and expect no response.
        if request_id is None:
            self._handle_notification(method, params)
            return None

        try:
            handler = self._METHODS.get(method)
            if handler is None:
                self.protocol_error_count += 1
                return self._error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")
            result = handler(self, params, credential)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except _ProtocolError as error:
            self.protocol_error_count += 1
            return self._error(request_id, error.code, error.message)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/initialized":
            self._initialized = True
        elif method == "notifications/cancelled":
            request_id = params.get("requestId")
            if request_id is not None:
                self._cancelled_requests.add(request_id)
            self._log.info("mcp.cancelled", extra={"request_id": request_id})
        # Other notifications are accepted and ignored.

    # -------------------------------------------------------------- MCP methods
    def _initialize(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self._initialized = True
        return {
            "protocolVersion": version,
            "capabilities": self.server_capabilities(),
            "serverInfo": SERVER_INFO,
        }

    def _ping(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        return {}

    def _tools_list(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        scopes = self._server.tool_scopes()
        tools = []
        for name in self._server.tool_names():
            required = _TOOL_REQUIRED.get(name, [])
            tools.append(
                {
                    "name": name,
                    "description": f"{name} (requires scope {scopes[name]})",
                    "inputSchema": {
                        "type": "object",
                        "properties": {key: {} for key in required},
                        "required": required,
                        "additionalProperties": True,
                    },
                }
            )
        return {"tools": tools}

    def _tools_call(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise _ProtocolError(INVALID_PARAMS, "tool name is required")
        try:
            result = self._server.call(name, credential=credential or "", arguments=arguments)
        except (McpAuthenticationError, McpAuthorizationError) as error:
            raise _ProtocolError(SERVER_ERROR, str(error)) from error
        except McpUnknownToolError as error:
            raise _ProtocolError(METHOD_NOT_FOUND, str(error)) from error
        except McpToolError as error:
            # Tool execution errors are returned as MCP results with isError=true.
            return {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": _json(result)}],
            "structuredContent": result,
            "isError": False,
        }

    def _resources_list(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        return {"resourceTemplates": resource_registry.list_resources()}

    def _resources_read(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise _ProtocolError(INVALID_PARAMS, "uri is required")
        principal = self._authenticate(credential)
        try:
            return resource_registry.read_resource(
                uri,
                scopes=set(principal.scopes),
                service=self._server._service,
                session_factory=self._server._session_factory,
                principal_name=principal.name,
                principal_id=principal.id,
            )
        except resource_registry.ResourceScopeError as error:
            raise _ProtocolError(SERVER_ERROR, str(error)) from error
        except resource_registry.ResourceError as error:
            raise _ProtocolError(INVALID_PARAMS, str(error)) from error

    def _prompts_list(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        return {"prompts": prompt_registry.list_prompts()}

    def _prompts_get(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        name = params.get("name")
        self._authenticate(credential)
        try:
            return prompt_registry.get_prompt(name, params.get("arguments"))
        except prompt_registry.PromptError as error:
            raise _ProtocolError(INVALID_PARAMS, str(error)) from error

    def _shutdown_method(self, params: dict[str, Any], credential: str | None) -> dict[str, Any]:
        self.request_shutdown()
        return {}

    # ------------------------------------------------------------------ helpers
    def _authenticate(self, credential: str | None):
        try:
            return self._server.authenticate(credential or "")
        except McpAuthenticationError as error:
            raise _ProtocolError(SERVER_ERROR, str(error)) from error

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    _METHODS: ClassVar[dict[str, Any]] = {
        "initialize": _initialize,
        "ping": _ping,
        "tools/list": _tools_list,
        "tools/call": _tools_call,
        "resources/list": _resources_list,
        "resources/read": _resources_read,
        "prompts/list": _prompts_list,
        "prompts/get": _prompts_get,
        "shutdown": _shutdown_method,
    }


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
