from __future__ import annotations

import sys
from typing import Iterable

from mcp.server.fastmcp import FastMCP

from .tools import BinaryResolver, ToolkitTool, build_tool_definitions


def get_tool_definitions(resolver: BinaryResolver | None = None) -> list[ToolkitTool]:
    """Return the MCP tool definitions in advertised order."""
    return build_tool_definitions(resolver or BinaryResolver())


def create_server(resolver: BinaryResolver | None = None) -> FastMCP:
    """Create the stdio MCP server."""
    active_resolver = resolver or BinaryResolver()
    server = FastMCP(
        name="agent-toolkit",
        instructions=(
            "Expose the agent-toolkit system-janitor and system-updater "
            "machine-mode contracts as MCP tools. Read-only report/health/version "
            "surfaces are safe by default; run tools keep destructive flags opt-in."
        ),
        website_url="https://github.com/agent-frontier/agent-toolkit",
        json_response=True,
    )

    for tool in get_tool_definitions(active_resolver):
        tool.register(server)

    return server


def log_resolved_binaries(resolver: BinaryResolver) -> None:
    """Log binary resolution to stderr without affecting stdio JSON-RPC."""
    for resolved in resolver.resolve_all().values():
        location = str(resolved.path) if resolved.path else "not found"
        source = f" ({resolved.source})" if resolved.source else ""
        print(f"[agent-toolkit-mcp] {resolved.display_name}: {location}{source}", file=sys.stderr, flush=True)
