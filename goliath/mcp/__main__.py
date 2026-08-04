"""Launcher for the Goliath MCP server.

Speaks the standards-compliant MCP JSON-RPC 2.0 protocol over a line-delimited
stdio transport (one JSON-RPC message per line). Supports tool discovery,
resources, prompts, protocol-version negotiation, cancellation, and graceful
shutdown. An optional legacy ``--simple`` mode retains the earlier
newline-delimited ``{"tool": ...}`` loop for backwards compatibility.

No network is opened by ``stdio``. The service-principal credential is read from
``GOLIATH_MCP_CREDENTIAL`` and never echoed. All authorization, scoping, and
auditing happen in ``GoliathMcpServer``/``GoliathMcpProtocol``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from goliath.config import load_config
from goliath.db.database import build_session_factory, create_production_engine
from goliath.mcp.protocol import GoliathMcpProtocol
from goliath.mcp.server import GoliathMcpServer, McpError


def _build_server() -> GoliathMcpServer:
    config = load_config()
    engine = create_production_engine()
    return GoliathMcpServer(session_factory=build_session_factory(engine), config=config)


def _run_protocol_stdio(server: GoliathMcpServer, credential: str) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s"
    )
    protocol = GoliathMcpProtocol(server)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = protocol.handle(message, credential=credential)
        if response is not None:
            json.dump(response, sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
        if protocol.is_shutting_down:
            break
    return 0


def _run_simple_stdio(server: GoliathMcpServer, credential: str) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result = server.call(
                request["tool"], credential=credential, arguments=request.get("arguments", {})
            )
            response = {"ok": True, "result": result}
        except (McpError, KeyError, ValueError) as error:
            response = {"ok": False, "error": str(error)}
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goliath.mcp", description="Goliath MCP server")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.add_argument("--simple", action="store_true", help="Use the legacy simple loop.")
    parser.add_argument(
        "--list-tools", action="store_true", help="Print the tool manifest and exit."
    )
    args = parser.parse_args(argv)

    server = _build_server()
    if args.list_tools:
        json.dump({"tools": server.tool_scopes()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    credential = os.environ.get("GOLIATH_MCP_CREDENTIAL", "")
    if not credential:
        sys.stderr.write("GOLIATH_MCP_CREDENTIAL is required\n")
        return 2

    if args.simple:
        return _run_simple_stdio(server, credential)
    return _run_protocol_stdio(server, credential)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
