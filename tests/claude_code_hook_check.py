"""Offline checks for the Claude Code hook adapter.

Feeds Claude Code-like JSON into the adapter and verifies that the adapter
remains a thin plug over the core memory engine. Does not launch Claude Code.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.claude_code_hook import handle_payload
from memoria_mcp.lifecycle import window_latch_file
from memoria_mcp.server import MemoriaRouter


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _additional_context(output: dict[str, Any]) -> str:
    hook_output = output.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return ""
    return str(hook_output.get("additionalContext") or "")


def run_check() -> dict[str, Any]:
    marker = f"claude_hook_{int(time.time() * 1000)}"
    with tempfile.TemporaryDirectory(prefix="ripple-claude-hook-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        data_dir = root / "memory-data"

        old_env = os.environ.copy()
        try:
            os.environ["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
            os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
            os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_SEARCH_MODE"] = "live"
            os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
            os.environ["RIPPLE_MEMORY_PROJECT"] = "claude_adapter_project"
            os.environ.pop("RIPPLE_MEMORY_HOOK_ENABLED", None)

            router = MemoriaRouter(str(data_dir))
            try:
                remember = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": "claude_adapter_project",
                        "content": (
                            f"{marker}: Claude Code hook adapter should emit context "
                            "without owning memory business logic."
                        ),
                        "type": "arch_decision",
                        "importance": 0.9,
                        "confidence": 0.95,
                    },
                )
                _assert(remember.get("stored") is True, f"setup remember failed: {remember}")
            finally:
                router.close()

            # --- Test 1: UserPromptSubmit ---
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-claude-1",
                "cwd": str(workspace),
                "prompt": f"Continue testing {marker} for Claude Code adapter",
            }
            output = handle_payload(payload)
            context = _additional_context(output)
            hook_specific = output.get("hookSpecificOutput")
            _assert(isinstance(hook_specific, dict), f"missing Claude hookSpecificOutput: {output}")
            _assert(
                hook_specific.get("hookEventName") == "UserPromptSubmit",
                f"wrong Claude hook event name: {output}",
            )
            _assert(marker in context, f"context missing recalled marker: {output}")
            _assert("session-claude-1" in context, "context missing session-derived window identity")

            latch = window_latch_file(
                cwd=workspace,
                project="claude_adapter_project",
                window_id="session-claude-1",
                data_dir=data_dir,
            )
            _assert(latch.is_file(), f"latch not written: {latch}")
            latch_text = latch.read_text(encoding="utf-8")
            _assert(marker in latch_text, "latch missing original user prompt")

            # --- Test 2: Stop (should be no-op) ---
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "session-claude-1",
            }
            stop_output = handle_payload(stop_payload)
            _assert(
                not _additional_context(stop_output),
                f"stop emitted additionalContext: {stop_output}",
            )

            # --- Test 3: Kill switch ---
            os.environ["RIPPLE_MEMORY_HOOK_ENABLED"] = "0"
            disabled_output = handle_payload(payload)
            _assert(
                not _additional_context(disabled_output),
                f"disabled hook still emitted context: {disabled_output}",
            )

            return {
                "ok": True,
                "workspace": str(workspace),
                "data_dir": str(data_dir),
                "context_contains_marker": marker in context,
                "latch_written": latch.is_file(),
                "claude_schema": "hookSpecificOutput.additionalContext",
                "stop_no_context": not _additional_context(stop_output),
                "disabled_no_context": not _additional_context(disabled_output),
            }
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def main() -> int:
    print(json.dumps(run_check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
