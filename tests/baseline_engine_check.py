"""Baseline regression check for Ripple Memory core.

This test intentionally uses the in-process router rather than an MCP client so
it can run fast before and after hook-adapter work. It verifies that the hook
layer has not broken the core remember/recall/read/forget contract.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.types import ListToolsRequest

from memoria_mcp.server import MemoriaRouter


EXPECTED_CORE_TOOLS = [
    "memoria_remember",
    "memoria_recall",
    "memoria_read",
    "memoria_forget",
]


async def _list_tool_names(router: MemoriaRouter) -> list[str]:
    handler = router.server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _contains_marker_in_jsonl(root: Path, marker: str) -> bool:
    for path in root.rglob("*.jsonl"):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


async def run_check() -> dict[str, Any]:
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
    os.environ.pop("MEMORIA_MCP_EXPOSE_PROJECT_TOOLS", None)

    marker = f"baseline_marker_{int(time.time() * 1000)}"
    project = "baseline_project"
    content = (
        f"{marker}: Ripple hook adapter baseline must preserve remember, "
        "recall, read, and hard forget behavior."
    )

    with tempfile.TemporaryDirectory(prefix="ripple-memory-baseline-") as tmp:
        data_dir = Path(tmp)
        router = MemoriaRouter(str(data_dir))
        try:
            tool_names = await _list_tool_names(router)
            _assert(tool_names == EXPECTED_CORE_TOOLS, f"unexpected tool list: {tool_names}")

            remember = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": content,
                    "type": "arch_decision",
                    "importance": 0.85,
                    "confidence": 0.95,
                },
            )
            _assert(remember.get("stored") is True, f"remember failed: {remember}")
            node_id = str(remember.get("node_id") or "")
            _assert(node_id, "remember did not return node_id")

            recall = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 3},
            )
            _assert(recall.get("count", 0) >= 1, f"recall returned no hits: {recall}")
            first = recall["results"][0]
            ref_id = first.get("ref_id")
            _assert(ref_id and first.get("read_hint"), f"recall missing ref/read_hint: {first}")

            read = router._dispatch_tool(
                "memoria_read",
                {"project": project, "ref_id": ref_id, "max_chars": 2000},
            )
            _assert(read.get("ok") is True, f"read failed: {read}")
            _assert(marker in read.get("text", ""), "read did not hydrate exact content")

            forget = router._dispatch_tool(
                "memoria_forget",
                {"project": project, "node_id": node_id},
            )
            _assert(forget.get("deleted") is True, f"forget failed: {forget}")
            _assert(forget.get("readable_after_delete") is False, f"forget not marked unreadable: {forget}")

            read_after = router._dispatch_tool(
                "memoria_read",
                {"project": project, "ref_id": ref_id, "max_chars": 2000},
            )
            _assert(read_after.get("ok") is False, f"deleted memory still readable: {read_after}")
            _assert(
                not _contains_marker_in_jsonl(data_dir, marker),
                "deleted memory marker still appears in JSONL stream",
            )

            return {
                "ok": True,
                "data_dir": str(data_dir),
                "tools": tool_names,
                "node_id": node_id,
                "recall_count": recall.get("count", 0),
                "deleted": forget.get("deleted"),
                "jsonl_marker_present_after_delete": False,
            }
        finally:
            router.close()


def main() -> int:
    result = asyncio.run(run_check())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
