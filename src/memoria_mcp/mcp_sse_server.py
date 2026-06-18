"""MCP-over-SSE server for direct agent-to-daemon connections.

When the host agent supports MCP via SSE or streamable-HTTP transport,
it can connect directly to the daemon without a stdio proxy process.

Usage:
    Agents that support URL-based MCP servers can configure:
        url = "http://127.0.0.1:<port>/sse"
    instead of launching a subprocess per window.

This eliminates the per-window proxy process overhead entirely.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any, Callable, Dict, Optional

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent

from .server import _json_text_for_mcp, _read_bool_env
from .tool_specs import build_memory_tools

logger = logging.getLogger("RippleMemory.McpSSE")

SSE_ENDPOINT = "/sse"
MESSAGE_ENDPOINT = "/messages"


def _find_free_port(host: str) -> int:
    """Bind to port 0 and return the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _create_mcp_app(
    tool_handler: Callable[[str, Dict[str, Any]], Any],
    *,
    expose_project_tools: bool = False,
) -> Any:
    """Create a starlette ASGI app that serves MCP over SSE.

    Args:
        tool_handler: Callable(tool_name, arguments) -> result dict.
        expose_project_tools: Whether to expose list_projects / archive_project.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    sse_transport = SseServerTransport(MESSAGE_ENDPOINT)
    mcp_server = Server("ripple-memory-daemon")

    @mcp_server.list_tools()
    async def list_tools():
        return build_memory_tools(expose_project_tools=expose_project_tools)

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        try:
            result = await asyncio.to_thread(tool_handler, name, dict(arguments or {}))
            return [TextContent(type="text", text=_json_text_for_mcp(result, indent=2))]
        except Exception as exc:
            logger.error("SSE tool %s failed: %s", name, exc)
            return [TextContent(type="text", text=_json_text_for_mcp({
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }))]

    async def handle_sse(request: Request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1],
                mcp_server.create_initialization_options(),
            )
        return Response()

    async def handle_message(request: Request):
        await sse_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
        return Response()

    app = Starlette(
        routes=[
            Route(SSE_ENDPOINT, endpoint=handle_sse),
            Route(MESSAGE_ENDPOINT, endpoint=handle_message, methods=["POST"]),
        ],
    )
    return app


class DaemonSSEServer:
    """Runs an MCP-over-SSE HTTP server in a background thread."""

    def __init__(
        self,
        tool_handler: Callable[[str, Dict[str, Any]], Any],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        expose_project_tools: bool = False,
    ):
        self._host = host
        self._requested_port = port
        self._actual_port: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._ready = threading.Event()
        self._app = _create_mcp_app(
            tool_handler,
            expose_project_tools=expose_project_tools,
        )

    @property
    def port(self) -> Optional[int]:
        """The actual port after start, or None if not started."""
        return self._actual_port

    def start(self) -> None:
        """Start the SSE server in a daemon thread."""
        import uvicorn

        # Resolve port: auto-assign if 0, otherwise use requested
        port = self._requested_port if self._requested_port > 0 else _find_free_port(self._host)
        self._actual_port = port

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        # Disable signal handlers — we're in a thread
        self._server.install_signal_handlers = lambda: None

        def _run():
            asyncio.run(self._server.serve())

        self._thread = threading.Thread(target=_run, daemon=True, name="ripple-mcp-sse")
        self._thread.start()
        self._ready.set()
        logger.info("MCP SSE server starting on %s:%s", self._host, port)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
