"""Minimal launcher for the Goliath MCP server.

Provides a simple newline-delimited JSON stdio transport so an agent host such as
Hermes can invoke bounded resale-domain tools. Each request line is a JSON object
``{"tool": "inventory.get", "arguments": {...}}``; each response line is a JSON
object ``{"ok": true, "result": {...}}`` or ``{"ok": false, "error": "..."}``.

No network is opened. The service-principal credential is read from the
``GOLIATH_MCP_CREDENTIAL`` environment variable and never echoed. This module is a
thin transport; all authorization, scoping, and auditing happen in
``GoliathMcpServer``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from goliath.config import load_config
from goliath.db.database import build_session_factory, create_production_engine
from goliath.mcp.server import GoliathMcpServer, McpError


def _build_server() -> GoliathMcpServer:
    config = load_config()
    engine = create_production_engine()
    return GoliathMcpServer(session_factory=build_session_factory(engine), config=config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goliath.mcp", description="Goliath MCP server")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
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

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result = server.call(
                request["tool"],
                credential=credential,
                arguments=request.get("arguments", {}),
            )
            response = {"ok": True, "result": result}
        except (McpError, KeyError, ValueError) as error:
            response = {"ok": False, "error": str(error)}
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
