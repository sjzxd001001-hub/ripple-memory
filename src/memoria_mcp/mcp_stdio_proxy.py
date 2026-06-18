"""Thin stdio MCP proxy for hosts that still launch one process per window.

The proxy exposes the canonical MCP tools, but all execution is forwarded to
the agent-level daemon. It must not own memory business logic, graph cache,
SQLite state, embedding models, or write queues.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from .daemon_client import call_daemon_tool, default_data_dir, ensure_agent_daemon
from .server import _json_text_for_mcp, _read_bool_env
from .tool_specs import build_memory_tools


logger = logging.getLogger("RippleMemory.McpProxy")


def _proxy_data_dir() -> Path:
    return default_data_dir()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = Server("ripple-memory")
    expose_project_tools = _read_bool_env("MEMORIA_MCP_EXPOSE_PROJECT_TOOLS", False)

    @server.list_tools()
    async def list_tools():
        # Listing tools is normally the first host interaction, so ensure the
        # shared daemon is warmed before the first tool call.
        ensure_agent_daemon(_proxy_data_dir(), timeout_seconds=3.0)
        return build_memory_tools(expose_project_tools=expose_project_tools)

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
        try:
            response = await asyncio.to_thread(call_daemon_tool, _proxy_data_dir(), name, dict(arguments or {}))
            if response.get("ok") and "result" in response:
                payload = response["result"]
            else:
                payload = response
            return [TextContent(type="text", text=_json_text_for_mcp(payload, indent=2))]
        except Exception as exc:  # noqa: BLE001 - MCP responses must stay structured.
            logger.error("Proxy tool %s failed: %s", name, exc)
            return [TextContent(type="text", text=_json_text_for_mcp({"error": str(exc), "error_type": exc.__class__.__name__}))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
